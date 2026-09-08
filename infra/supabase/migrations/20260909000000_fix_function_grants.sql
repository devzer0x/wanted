-- Repair: revoke EXECUTE on the server-only functions from `anon` and `authenticated` BY NAME.
--
-- Why a separate migration rather than an edit to 20260908120001.
--
-- That migration had already run against the live project, and a migration that has run does not
-- run again. Fixing it in place corrects fresh installs and leaves every database that already
-- applied it exposed, which is the database that matters. So the fix ships twice: in 120001 for
-- new installs, and here for the ones already created.
--
-- What was wrong.
--
-- 120001 said `revoke execute ... from public`. Two separate grants existed and that removed only
-- one of them:
--
--   1. PostgreSQL grants EXECUTE on a new function to the PUBLIC pseudo-role by default.
--   2. Supabase ships `alter default privileges in schema public grant all on functions to anon,
--      authenticated, service_role`, which grants EXECUTE to those roles DIRECTLY.
--
-- Revoking from PUBLIC does nothing to a direct grant. A plain Postgres has no (2), so the local
-- verification passed while the real project left `enter_prediction` callable by `anon` — meaning
-- anyone holding the publishable key, which ships to every browser, could submit entries for a
-- wallet they did not own, bypassing sign-in and the API's rate limits. `lock_due_predictions` and
-- `settle_due_predictions` were callable too (both idempotent and window-bounded, so the exposure
-- there was timing rather than data).
--
-- Confirmed against the live project before the fix: `enter_prediction` returned `false` and the
-- two tick functions returned `0` to an `anon` caller instead of `42501`. The money functions
-- (`create_reward_claim`, `finalize_reward_claim`) were never exposed — their own migration
-- happened to name `anon, authenticated` in its revoke, which is the pattern this one adopts.
--
-- infra/verify-predictions.sh now installs the same default privileges, so a local run reproduces
-- Supabase instead of hiding this. Verified: with the old `from public` revoke the check reports
-- 0 of 3 functions denied; with the fix, 3 of 3.
--
-- Idempotent and safe to re-run.

revoke execute on function public.enter_prediction(uuid, text, text) from public, anon, authenticated;
revoke execute on function public.lock_due_predictions() from public, anon, authenticated;
revoke execute on function public.settle_due_predictions() from public, anon, authenticated;

-- Re-assert the intended grant; the revokes above are broad on purpose.
grant execute on function public.enter_prediction(uuid, text, text) to service_role;
grant execute on function public.lock_due_predictions() to service_role;
grant execute on function public.settle_due_predictions() to service_role;

-- `leaderboard()` is a deliberate public read surface and keeps its grant.
grant execute on function public.leaderboard(text) to anon, authenticated, service_role;

-- Belt and braces for anything added later under the same default privileges: stop new objects in
-- this schema from being granted to the browser roles automatically. Explicit grants above still
-- apply; this only removes the blanket default.
alter default privileges in schema public revoke all on functions from anon, authenticated;
