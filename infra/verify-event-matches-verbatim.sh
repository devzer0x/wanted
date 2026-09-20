#!/usr/bin/env bash
# Proves that 20260921000000_event_matches.sql changes settle_due_predictions() by EXACTLY one
# branch and nothing else.
#
# Postgres has no "add a branch" DDL, so that migration has to resubmit the whole 400-line function.
# That is the one thing about it worth distrusting: a stray edit anywhere in the copy would ship
# silently inside a `create or replace`. This script extracts both function definitions from the
# migrations themselves and refuses to pass if the new body removes a single line, or if what it
# adds is anything other than the `event_matches` branch.
#
# Needs no database. Usage: cd infra && ./verify-event-matches-verbatim.sh
set -euo pipefail
cd "$(dirname "$0")"

OLD=supabase/migrations/20260908120000_predictions.sql
NEW=supabase/migrations/20260921000000_event_matches.sql
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# The statement, located by its own text rather than by line number, so this keeps working (or
# fails loudly) if either file moves.
awk '/^create function public\.settle_due_predictions\(\)$/,/^\$\$;$/'            "$OLD" > "$TMP/old_stmt.sql"
awk '/^create or replace function public\.settle_due_predictions\(\)$/,/^\$\$;$/' "$NEW" > "$TMP/new_stmt.sql"
for f in old new; do
  [[ -s "$TMP/${f}_stmt.sql" ]] || { echo "FAIL: could not find the $f definition" >&2; exit 1; }
  # The body is the $$-quoted part; everything before it is the signature.
  sed -n '/^as \$\$$/,/^\$\$;$/p' "$TMP/${f}_stmt.sql" > "$TMP/${f}_body.sql"
done

echo "== signature, return type, language, security and search_path =="
diff <(sed -n '2,5p' "$TMP/old_stmt.sql") <(sed -n '2,5p' "$TMP/new_stmt.sql") \
  || { echo "FAIL: the new definition changes the function's signature or settings" >&2; exit 1; }
sed -n '2,5p' "$TMP/new_stmt.sql" | sed 's/^/   /'
echo "   (the only difference outside the body: $(head -1 "$TMP/old_stmt.sql") -> $(head -1 "$TMP/new_stmt.sql"))"

echo
echo "== diff of the function BODIES (old -> new) =="
diff -u "$TMP/old_body.sql" "$TMP/new_body.sql" || true

REMOVED=$(diff "$TMP/old_body.sql" "$TMP/new_body.sql" | grep -c '^<' || true)
ADDED=$(diff "$TMP/old_body.sql" "$TMP/new_body.sql" | grep -c '^>' || true)
HUNKS=$(diff -u "$TMP/old_body.sql" "$TMP/new_body.sql" | grep -c '^@@' || true)
# `|| true` on each of these because `diff` exits 1 when there ARE differences, which under
# `pipefail` would take the whole script down before it could judge them.
FIRST=$(diff "$TMP/old_body.sql" "$TMP/new_body.sql" | grep '^>' | head -1 | cut -c3- || true)

echo
echo "   lines removed: $REMOVED   (must be 0)"
echo "   lines added:   $ADDED"
echo "   hunks:         $HUNKS   (must be 1)"
echo "   first added:   $FIRST"

[[ "$REMOVED" == "0" ]] || { echo "FAIL: the copy is not verbatim — it drops lines." >&2; exit 1; }
[[ "$HUNKS" == "1" ]]   || { echo "FAIL: the change is not a single contiguous branch." >&2; exit 1; }
[[ "$FIRST" == "      elsif v_kind = 'event_matches'" ]] \
  || { echo "FAIL: what was added does not start the event_matches branch." >&2; exit 1; }
grep -q "payload @> (v_params -> 'payload_match')" "$TMP/new_body.sql" \
  || { echo "FAIL: the added branch does not apply the payload filter." >&2; exit 1; }

echo
echo "PASS: the new body is the current definition verbatim plus exactly the event_matches branch."
