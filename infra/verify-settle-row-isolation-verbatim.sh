#!/usr/bin/env bash
# Proves that 20260922000000_settle_row_isolation.sql changes settle_due_predictions() by EXACTLY the
# four hunks declared below, and by nothing else.
#
# Postgres has no "patch a function" DDL, so that migration resubmits the whole 400-line function.
# That is the one thing about it worth distrusting: a stray edit anywhere in the copy would ship
# silently inside a `create or replace`, on the path that writes the reward ledger. So this script
# does not describe the change, it DECLARES it: it takes the current definition (the one in
# 20260921000000_event_matches.sql), applies the four hunks below at their anchor lines, and
# requires the new migration's statement to be that result byte for byte. Any other difference —
# one character, one trailing space, anywhere — fails.
#
#   hunk 1  `begin` of the per-candidate block, before `v_void_reason := null;`       (row isolation)
#   hunk 2  the max_per_prediction clamp record, after `v_clamp_reason := null;`       (the clamp)
#   hunk 3  the daily_cap reason appends instead of overwriting (the one replaced line)  (the clamp)
#   hunk 4  the class-22 handler and the block's `end;`, before `  end loop;`           (row isolation)
#
# It then runs NEGATIVE CONTROLS: copies of the new migration with a one-line stray edit, a handler
# widened to `others`, SQLERRM leaked into the public evidence, a trailing space, and an appended
# grant. Every one of them must be rejected, or this script fails — a check that cannot fail
# proves nothing.
#
# Needs no database. Usage: cd infra && ./verify-settle-row-isolation-verbatim.sh
set -euo pipefail
cd "$(dirname "$0")"

OLD=supabase/migrations/20260921000000_event_matches.sql
NEW=supabase/migrations/20260922000000_settle_row_isolation.sql
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---- the change, declared ----------------------------------------------------------------------
cat > "$TMP/hunk1.sql" <<'HUNK'
    -- 20260922000000 — row isolation. From here to the `exception` clause just above `end loop`, a
    -- candidate runs in its own subtransaction: a DATA error (SQLSTATE class 22) in one prediction
    -- voids that prediction and the loop moves on, instead of aborting the call for every row.
    -- The claim above stays OUTSIDE the block on purpose: it is what guarantees the row is
    -- `resolving` when the handler runs, the one state the status guard lets become `void`. The
    -- lines below are deliberately not re-indented, so that the diff against 20260921000000 is
    -- exactly the hunks infra/verify-settle-row-isolation-verbatim.sh declares.
    begin

HUNK
cat > "$TMP/hunk2.sql" <<'HUNK'

        -- §6, 20260922000000: the pool was already cut to max_per_prediction above, before the
        -- split, so every credit paid from a cut pool is a clamped credit and is recorded as one.
        -- The two caps below append their reasons to this one.
        if v_effective_pool_base < v_pool_base then
          v_clamped := true;
          v_clamp_reason := 'max_per_prediction';
        end if;
HUNK
cat > "$TMP/hunk3.sql" <<'HUNK'
          v_clamp_reason := coalesce(v_clamp_reason || '+daily_cap', 'daily_cap');
HUNK
cat > "$TMP/hunk4.sql" <<'HUNK'

    exception when data_exception then
      -- Class 22 ONLY (a category name matches every code in its class): bad data in this row's
      -- rule or in the events it reads, which no retry can fix. Every other class — 40001, 40P01,
      -- 57014, 55P03, 53xxx, the status guard's P0001 — still propagates and aborts the call, so a
      -- transient failure is retried by the next tick instead of voiding a winnable round.
      --
      -- The subtransaction has already undone this candidate's partial work: its ledger rows, its
      -- streak updates and any `settled` write. The row is therefore `resolving` again, and
      -- resolving -> void is allowed; the status guard stamps settled_at, as for every void.
      --
      -- The evidence carries the error CODE and nothing else. resolution_evidence is public
      -- (predictions RLS) and the server's message quotes the offending input, so it stays out
      -- (CONTRACTS-PREDICTIONS §10.1 principle 7). The warning is for the operator's log, and
      -- carries the same two facts: which prediction, which code.
      update public.predictions
      set status = 'void',
          resolution_evidence = jsonb_build_object('void_reason', 'settlement_error', 'sqlstate', sqlstate)
      where id = p.id;
      raise warning 'settle_due_predictions: prediction % voided as settlement_error, SQLSTATE %',
        p.id, sqlstate;
      v_settled_count := v_settled_count + 1;
    end;
HUNK

ANCHOR1='    v_void_reason := null;'
ANCHOR2='        v_clamp_reason := null;'
ANCHOR3="          v_clamp_reason := 'daily_cap';"
ANCHOR4='  end loop;'

# The statement, located by its own text rather than by line number.
extract_stmt() { awk '/^create or replace function public\.settle_due_predictions\(\)$/,/^\$\$;$/' "$1"; }

extract_stmt "$OLD" > "$TMP/old_stmt.sql"
[[ -s "$TMP/old_stmt.sql" ]] || { echo "FAIL: could not find the current definition in $OLD" >&2; exit 1; }
[[ -f "$NEW" ]] || { echo "FAIL: $NEW does not exist" >&2; exit 1; }

# Each anchor must occur exactly once, or "apply the hunk at the anchor" would be ambiguous.
for a in "$ANCHOR1" "$ANCHOR2" "$ANCHOR3" "$ANCHOR4"; do
  n=$(grep -cxF -- "$a" "$TMP/old_stmt.sql" || true)
  [[ "$n" == "1" ]] || { echo "FAIL: anchor occurs $n times in the current definition: [$a]" >&2; exit 1; }
done

# Current definition + the four hunks at their anchors = what the new statement must be.
awk -v h1="$TMP/hunk1.sql" -v h2="$TMP/hunk2.sql" -v h3="$TMP/hunk3.sql" -v h4="$TMP/hunk4.sql" \
    -v a1="$ANCHOR1" -v a2="$ANCHOR2" -v a3="$ANCHOR3" -v a4="$ANCHOR4" '
  function emit(f,   line) { while ((getline line < f) > 0) print line; close(f) }
  $0 == a1 { emit(h1); print; next }
  $0 == a2 { print; emit(h2); next }
  $0 == a3 { emit(h3); next }
  $0 == a4 { emit(h4); print; next }
  { print }
' "$TMP/old_stmt.sql" > "$TMP/expected_stmt.sql"

# check FILE: 0 only if FILE is comments + exactly the expected statement, byte for byte.
check() {
  local cand="$1"
  extract_stmt "$cand" > "$TMP/cand_stmt.sql"
  [[ -s "$TMP/cand_stmt.sql" ]] || { echo "     no settle_due_predictions() statement found"; return 1; }
  # Nothing but comments and blank lines outside the statement: no grant, no revoke, no second
  # statement, no edit to the serialised wrapper.
  awk '/^create or replace function public\.settle_due_predictions\(\)$/,/^\$\$;$/ { next } { print }' "$cand" \
    | grep -vE '^[[:space:]]*(--.*)?$' > "$TMP/outside.txt" || true
  if [[ -s "$TMP/outside.txt" ]]; then
    echo "     SQL outside the one statement:"; sed 's/^/       /' "$TMP/outside.txt"; return 1
  fi
  if ! cmp -s "$TMP/expected_stmt.sql" "$TMP/cand_stmt.sql"; then
    echo "     differs from (current definition + the four declared hunks):"
    diff -u "$TMP/expected_stmt.sql" "$TMP/cand_stmt.sql" | sed -n '3,$p' | grep -E '^[-+]' | sed 's/^/       /' | head -12
    return 1
  fi
  return 0
}

extract_stmt "$NEW" > "$TMP/new_stmt.sql"

echo "== signature, return type, language, security and search_path =="
diff <(sed -n '1,5p' "$TMP/old_stmt.sql") <(sed -n '1,5p' "$TMP/new_stmt.sql") \
  || { echo "FAIL: the new definition changes the function's signature or settings" >&2; exit 1; }
sed -n '1,5p' "$TMP/new_stmt.sql" | sed 's/^/   /'

echo
echo "== diff of the statements (20260921000000 -> 20260922000000), for review =="
diff -u "$TMP/old_stmt.sql" "$TMP/new_stmt.sql" || true

REMOVED=$(diff "$TMP/old_stmt.sql" "$TMP/new_stmt.sql" | grep -c '^<' || true)
ADDED=$(diff "$TMP/old_stmt.sql" "$TMP/new_stmt.sql" | grep -c '^>' || true)
GONE=$(diff "$TMP/old_stmt.sql" "$TMP/new_stmt.sql" | grep '^<' | cut -c3- || true)
echo
echo "   lines removed: $REMOVED   (must be 1: the daily_cap reason, which now appends instead of overwriting)"
echo "   removed line:  $GONE"
echo "   lines added:   $ADDED"
[[ "$REMOVED" == "1" && "$GONE" == "$ANCHOR3" ]] \
  || { echo "FAIL: the new body removes something other than the one daily_cap reason line" >&2; exit 1; }

echo
echo "== the statement is the current definition plus exactly the four declared hunks =="
if check "$NEW"; then
  echo "   ok — byte-identical to 20260921000000's statement with hunks 1-4 applied"
else
  echo "FAIL: $NEW is not the current definition plus exactly the declared hunks" >&2; exit 1
fi

echo
echo "== what the hunks say, checked on the installed text rather than trusted from the comments =="
CODE=$(sed 's/--.*$//' "$TMP/new_stmt.sql")
HANDLERS=$(grep -ciE '^[[:space:]]*exception[[:space:]]+when' <<<"$CODE" || true)
echo "   exception handlers in the body: $HANDLERS (must be 1)"
[[ "$HANDLERS" == "1" ]] || { echo "FAIL: expected exactly one exception handler" >&2; exit 1; }
grep -qxF '    exception when data_exception then' <<<"$CODE" \
  || { echo "FAIL: the handler is not \`when data_exception\` (SQLSTATE class 22 only)" >&2; exit 1; }
echo "   ok — the one handler is \`when data_exception\` (class 22 only)"
if grep -qiE 'sqlerrm|when[[:space:]]+others|message_text|pg_exception_detail' <<<"$CODE"; then
  echo "FAIL: the body reads the error message or traps every class" >&2; exit 1
fi
echo "   ok — no SQLERRM / MESSAGE_TEXT / PG_EXCEPTION_DETAIL, no WHEN OTHERS in the code"
grep -qF "jsonb_build_object('void_reason', 'settlement_error', 'sqlstate', sqlstate)" <<<"$CODE" \
  || { echo "FAIL: the void evidence is not exactly {void_reason, sqlstate}" >&2; exit 1; }
echo "   ok — the void evidence is {void_reason: settlement_error, sqlstate: <code>}"

echo
echo "== NEGATIVE CONTROLS — each tampered copy MUST be rejected =="
neg() {  # neg NAME SED-EXPR [APPEND-LINE]
  local name="$1" expr="$2" append="${3:-}" f="$TMP/tampered.sql"
  sed "$expr" "$NEW" > "$f"
  [[ -n "$append" ]] && printf '%s\n' "$append" >> "$f"
  if cmp -s "$f" "$NEW"; then
    echo "FAIL: negative control '$name' did not change the file, so it proves nothing" >&2; exit 1
  fi
  echo "   [$name]"
  if check "$f"; then
    echo "FAIL: negative control '$name' was ACCEPTED — the verbatim check is not sharp" >&2; exit 1
  fi
  echo "     -> rejected, as it must be"
}
# A one-line stray edit far from any hunk, in the completeness gate that guards every payout.
neg "stray one-line edit: >= becomes > in the telemetry-completeness gate" \
    "s/^      v_ready := coalesce(v_max_events_ts, '-infinity'::timestamptz) >= p.resolves_at$/      v_ready := coalesce(v_max_events_ts, '-infinity'::timestamptz) > p.resolves_at/"
neg "handler widened from class 22 to every class" \
    "s/^    exception when data_exception then$/    exception when others then/"
neg "the server's message leaked into the public evidence" \
    "s/'sqlstate', sqlstate)$/'sqlstate', sqlstate, 'message', sqlerrm)/"
neg "whitespace only: one trailing space on an untouched line" \
    "s/^    v_settled_count := v_settled_count + 1;$/    v_settled_count := v_settled_count + 1; /"
# The statement untouched (an empty sed script), one line appended after it.
neg "a grant appended after the statement" \
    "" "grant execute on function public.settle_due_predictions() to service_role;"

echo
echo "PASS: 20260922000000 is the 20260921000000 definition verbatim plus exactly the four declared"
echo "      hunks, and every negative control was rejected."
