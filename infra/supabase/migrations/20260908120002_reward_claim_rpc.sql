-- WANTED — atomic claim creation. CONTRACTS-PREDICTIONS.md v2.0 §2 (invariant 4) and §6.
--
-- Why this function exists at all.
--
-- The web layer reaches Postgres only through PostgREST with the service-role key. That client
-- issues one statement per call and cannot hold a transaction open, so "create the claim row, then
-- mark the ledger rows as belonging to it" is unavoidably two round trips from the route handler.
-- Two round trips are not atomic: a crash, a timeout or a cold-start eviction between them leaves
-- a `pending` claim that owns no ledger rows, while the balance it was supposed to consume is
-- still claimable. That is a real double-spend window, not a theoretical one.
--
-- Pushing the whole operation into one function makes it a single statement from PostgREST's point
-- of view, so Postgres wraps it in one transaction and the two writes commit or fail together.
--
-- The function also takes the ledger selection away from the client. An earlier design had the
-- route pass the ledger row ids it had just read; those ids can go stale between the read and the
-- write (a settlement landing in between), and a client-supplied id list is a client-supplied
-- amount by another name. Here the function re-reads the wallet's own unclaimed rows under a lock
-- and sums them itself, so the amount is derived server-side from the ledger and nowhere else.

create function public.create_reward_claim(
  p_wallet text,
  p_min_amount numeric default null,
  p_max_amount numeric default null
)
returns public.reward_claims
language plpgsql
security definer
set search_path = public
as $$
declare
  v_wallet text := lower(trim(p_wallet));
  v_amount numeric(38, 18);
  v_asset text;
  v_assets integer;
  v_claim public.reward_claims;
begin
  if v_wallet is null or v_wallet = '' then
    raise exception 'wallet is required' using errcode = '22023';
  end if;

  -- Lock this wallet's unclaimed credits for the duration of the transaction. Without the lock,
  -- two concurrent claims could each read the same rows and each believe they own them; the
  -- partial unique index on reward_claims would reject the second INSERT, but only after the
  -- first had already been told a different total.
  perform 1
  from public.reward_ledger
  where wallet = v_wallet and claim_id is null
  for update;

  -- A claim pays out exactly one asset. Mixing assets into a single on-chain transfer would be
  -- wrong, so refuse rather than guess which one the operator meant.
  select count(distinct asset), min(asset), coalesce(sum(amount), 0)
    into v_assets, v_asset, v_amount
  from public.reward_ledger
  where wallet = v_wallet and claim_id is null;

  if coalesce(v_assets, 0) = 0 or v_amount <= 0 then
    raise exception 'nothing to claim' using errcode = 'P0002';
  end if;

  if v_assets > 1 then
    raise exception 'multiple reward assets pending; claim them separately'
      using errcode = '22023';
  end if;

  -- §6 rails. Passed in by the caller from env so the limits stay operator-configurable, but
  -- ENFORCED here, inside the transaction, where the amount is actually known.
  if p_min_amount is not null and v_amount < p_min_amount then
    raise exception 'below minimum claim amount' using errcode = 'P0003';
  end if;

  if p_max_amount is not null and v_amount > p_max_amount then
    raise exception 'above maximum claim amount; manual release required' using errcode = 'P0004';
  end if;

  -- The partial unique index (wallet) WHERE status IN ('pending','submitted') is what makes a
  -- replayed or concurrent claim fail here with 23505 rather than double-spend. The route turns
  -- that into a 409.
  insert into public.reward_claims (wallet, amount, asset, status)
  values (v_wallet, v_amount, v_asset, 'pending')
  returning * into v_claim;

  -- Same transaction: the credits now belong to this claim and are no longer claimable.
  update public.reward_ledger
     set claim_id = v_claim.id
   where wallet = v_wallet and claim_id is null;

  return v_claim;
end;
$$;

comment on function public.create_reward_claim(text, numeric, numeric) is
  'Atomically converts a wallet''s unclaimed reward_ledger credits into one pending claim. '
  'The amount is summed server-side from the ledger; callers cannot supply it.';

-- Finalising a claim. Kept as a function for the same reason: the status transition and the
-- tx hash must land together, and a failed claim must return its credits to the claimable pool
-- in the same transaction that marks it failed — otherwise a failed transfer silently burns a
-- viewer's rewards.
create function public.finalize_reward_claim(
  p_claim_id uuid,
  p_status text,
  p_tx_hash text default null,
  p_error text default null
)
returns public.reward_claims
language plpgsql
security definer
set search_path = public
as $$
declare
  v_claim public.reward_claims;
begin
  if p_status not in ('submitted', 'confirmed', 'failed') then
    raise exception 'invalid claim status %', p_status using errcode = '22023';
  end if;

  update public.reward_claims
     set status = p_status,
         tx_hash = coalesce(p_tx_hash, tx_hash),
         error = case when p_status = 'failed' then p_error else null end,
         updated_at = now()
   where id = p_claim_id
     -- Terminal states are terminal: a late callback cannot reopen a confirmed claim.
     and status in ('pending', 'submitted')
  returning * into v_claim;

  if not found then
    raise exception 'claim not found or already final' using errcode = 'P0002';
  end if;

  -- Release the credits back to claimable so the viewer can retry. Their reward was never
  -- delivered, so it must not stay attached to a dead claim.
  if p_status = 'failed' then
    update public.reward_ledger set claim_id = null where claim_id = v_claim.id;
  end if;

  return v_claim;
end;
$$;

comment on function public.finalize_reward_claim(uuid, text, text, text) is
  'Moves a claim to submitted/confirmed/failed. A failed claim returns its ledger credits to the '
  'claimable pool in the same transaction, so a failed transfer never burns rewards.';

-- Only the server may create or finalise a claim.
revoke all on function public.create_reward_claim(text, numeric, numeric) from public, anon, authenticated;
revoke all on function public.finalize_reward_claim(uuid, text, text, text) from public, anon, authenticated;
grant execute on function public.create_reward_claim(text, numeric, numeric) to service_role;
grant execute on function public.finalize_reward_claim(uuid, text, text, text) to service_role;
