-- Records which version of the public rules (/rules) a wallet accepted when it signed in.
--
-- POLICY_REQUIRE_TERMS has been on in production, and nothing anywhere recorded acceptance, so
-- policy reported every wallet "terms_not_confirmed" and nobody could have claimed a reward. The
-- acceptance itself is the SIWE signature: the signed statement names the rules version
-- (web/src/lib/auth/siwe.ts RULES_VERSION), and /api/auth/verify only succeeds against a message it
-- rebuilt with the current version. This column is the durable audit record of that fact, stamped
-- in the same UPDATE that burns the nonce, so a row with verified_at set and rules_version = N is
-- proof that this address signed a message accepting rules vN.
--
-- Additive and idempotent. NULL on every row that predates it, which reads as "not accepted" —
-- the correct reading, since those signatures named no rules.

alter table public.wallet_sessions add column if not exists rules_version integer;

comment on column public.wallet_sessions.rules_version is
  'The /rules version named in the SIWE statement this session signed. NULL = signed before rules '
  'acceptance existed; reads as not accepted. See web/src/lib/auth/siwe.ts RULES_VERSION.';
