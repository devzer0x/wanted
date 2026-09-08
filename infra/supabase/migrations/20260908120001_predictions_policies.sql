-- WASTED prediction layer RLS — CONTRACTS-PREDICTIONS.md v2.0 §2 "RLS". Same posture as
-- 20260825120001_policies.sql: RLS default-denies everything not covered by a policy, so the
-- absence of a policy on the sensitive tables below IS the lockdown; the explicit revokes are
-- defense-in-depth at the privilege layer, same as the original migration's own comment says.
--
-- Grants from 20260825120001_policies.sql only covered tables/functions that existed when it
-- ran; every object this migration adds needs its own grant.

alter table public.wallet_sessions    enable row level security;
alter table public.predictions        enable row level security;
alter table public.prediction_entries enable row level security;
alter table public.reward_claims      enable row level security;
alter table public.reward_ledger      enable row level security;
alter table public.wallet_streaks     enable row level security;
alter table public.policy_flags       enable row level security;

-- service_role keeps its usual full access, plus sequence usage for the identity-column tables.
grant all on public.wallet_sessions, public.predictions, public.prediction_entries,
  public.reward_claims, public.reward_ledger, public.wallet_streaks, public.policy_flags
  to service_role;
grant usage, select on public.prediction_entries_id_seq, public.reward_ledger_id_seq to service_role;

-- Public reads, exactly per CONTRACTS-PREDICTIONS §2 "RLS":
--   predictions      — public select where status <> 'open' OR opened_at <= now()
--   wallet_streaks    — public select
--   prediction_distribution / leaderboard — public, via the SECURITY DEFINER view/function
--                        defined in the schema migration (functions/views need their own grant,
--                        not a row policy)
-- reward_ledger, reward_claims, wallet_sessions, policy_flags — NO public policy at all. Reachable
-- only with the service-role key, server-side. The browser never reads them directly.
grant select on public.predictions to anon, authenticated;
grant select on public.wallet_streaks to anon, authenticated;

create policy predictions_public_read on public.predictions
  for select to anon, authenticated
  using (status <> 'open' or opened_at <= now());

create policy wallet_streaks_public_read on public.wallet_streaks
  for select to anon, authenticated
  using (true);

-- prediction_entries — public select of aggregate counts only, via the security-definer
-- `prediction_distribution` view. Raw entry rows are never publicly readable: no policy here, and
-- no table-level grant to anon/authenticated either.
-- service_role is included deliberately, and its absence here was a real outage: the browser reads
-- this view directly, but so does the server, because /api/predictions/live renders the
-- participation split with the service-role client. Omitting it made that whole route fail with
-- "permission denied for view prediction_distribution" while an `anon` smoke test passed happily.
-- The blanket `grant all on all tables ... to service_role` in the 2026-08-25 policies migration
-- does NOT cover this view: that grant applied to the tables existing at the time it ran.
grant select on public.prediction_distribution to anon, authenticated, service_role;

-- `leaderboard()` is the other public-select surface named in §2 ("the leaderboard view"); it is
-- implemented as a function (it takes a window argument) rather than a bare view.
grant execute on function public.leaderboard(text) to anon, authenticated, service_role;

-- `enter_prediction`, `lock_due_predictions`, `settle_due_predictions` are server-side only.
-- PostgreSQL grants EXECUTE on a newly created function to PUBLIC by default — revoke that first,
-- or anon/authenticated could call `enter_prediction` directly with an arbitrary `p_wallet` and
-- submit entries for a wallet they don't own (identity is verified by the API route before it
-- calls this function, not by the function itself; it only enforces the time lock).
revoke execute on function public.enter_prediction(uuid, text, text) from public;
revoke execute on function public.lock_due_predictions() from public;
revoke execute on function public.settle_due_predictions() from public;
grant execute on function public.enter_prediction(uuid, text, text) to service_role;
grant execute on function public.lock_due_predictions() to service_role;
grant execute on function public.settle_due_predictions() to service_role;

revoke insert, update, delete, truncate, references, trigger
  on public.wallet_sessions, public.predictions, public.prediction_entries,
     public.reward_claims, public.reward_ledger, public.wallet_streaks, public.policy_flags
  from anon, authenticated;

-- Belt-and-suspenders per CONTRACTS-PREDICTIONS §2: no SELECT at all for anon/authenticated on
-- the tables with no public policy (RLS already blocks every row; this blocks the grant too).
revoke select on public.wallet_sessions, public.reward_claims, public.reward_ledger,
  public.policy_flags, public.prediction_entries
  from anon, authenticated;
