\echo '== [1] anon can SELECT every public table (expect 7x count=0)'
set role anon;
select 'sessions' t, count(*) from public.sessions union all
select 'decisions', count(*) from public.decisions union all
select 'events', count(*) from public.events union all
select 'missions', count(*) from public.missions union all
select 'clips', count(*) from public.clips union all
select 'stats', count(*) from public.stats union all
select 'site_config', count(*) from public.site_config;

\echo '== [2] anon INSERT/UPDATE/DELETE must all FAIL (expect 3x permission denied)'
insert into public.events (session_id, type) values (gen_random_uuid(), 'death');
update public.stats set deaths = 99;
delete from public.decisions;
reset role;

\echo '== [3] service_role writes succeed'
set role service_role;
insert into public.sessions (game_edition, harness_version) values ('legacy', 'verify-0.0.0') returning id \gset
insert into public.events (session_id, type, payload) values (:'id', 'session_start', '{"harness_version":"verify-0.0.0"}');
insert into public.decisions (session_id, layer, thought, say, mood, goal, action, confidence, model)
  values (:'id', 'tactical', 'verify row', 'verify', 'chill', 'verify RLS', '{"type":"stop","params":{}}', 0.9, 'claude-haiku-4-5-20251001');
insert into public.stats (session_id) values (:'id')
  on conflict (session_id) do update set heartbeat_at = now();
insert into public.site_config (key, value) values ('stream', '{"provider":"twitch","channel":"placeholder-until-channel-exists","video_id":null}')
  on conflict (key) do update set value = excluded.value, updated_at = now();
reset role;

\echo '== [4] anon now sees the service_role rows (expect counts 1,1,1,1)'
set role anon;
select (select count(*) from public.sessions) sessions,
       (select count(*) from public.decisions) decisions,
       (select count(*) from public.events) events,
       (select count(*) from public.stats) stats;
reset role;

\echo '== [5] enum constraints reject bad values (expect 2x check violation)'
set role service_role;
insert into public.events (session_id, type) values (:'id', 'not_a_real_type');
insert into public.decisions (session_id, layer, thought, say, mood, goal, action, confidence, model)
  values (:'id', 'tactical', 'x', 'x', 'angry', 'x', '{}', 0.5, 'm');
reset role;

\echo '== [6] realtime publication contains exactly decisions, events, stats'
select schemaname, tablename from pg_publication_tables where pubname = 'supabase_realtime' order by tablename;
