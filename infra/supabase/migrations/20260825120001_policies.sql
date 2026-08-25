-- WASTED RLS — CONTRACTS.md v1.1 §5: public SELECT on every public table, no public writes.
-- The harness writes with the service-role/secret key (bypassrls). RLS default-denies everything
-- not covered by a policy, so the absence of insert/update/delete policies IS the write lockdown;
-- the explicit revokes below are defense-in-depth at the privilege layer.

alter table public.sessions    enable row level security;
alter table public.decisions   enable row level security;
alter table public.events      enable row level security;
alter table public.missions    enable row level security;
alter table public.clips       enable row level security;
alter table public.stats       enable row level security;
alter table public.site_config enable row level security;

create policy sessions_public_read    on public.sessions    for select to anon, authenticated using (true);
create policy decisions_public_read   on public.decisions   for select to anon, authenticated using (true);
create policy events_public_read      on public.events      for select to anon, authenticated using (true);
create policy missions_public_read    on public.missions    for select to anon, authenticated using (true);
create policy clips_public_read       on public.clips       for select to anon, authenticated using (true);
create policy stats_public_read       on public.stats       for select to anon, authenticated using (true);
create policy site_config_public_read on public.site_config for select to anon, authenticated using (true);

revoke insert, update, delete, truncate, references, trigger
  on all tables in schema public
  from anon, authenticated;
