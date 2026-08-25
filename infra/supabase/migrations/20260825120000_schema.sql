-- WASTED schema — CONTRACTS.md v1.1 §5. Migrations are the canonical schema source (infra/README).
-- All writes come from the harness via the service-role key; the public reads via RLS (policies migration).

create table public.sessions (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  game_edition text not null check (game_edition in ('legacy', 'enhanced')),
  harness_version text not null
);

create table public.decisions (
  id bigint generated always as identity primary key,
  session_id uuid not null references public.sessions (id),
  ts timestamptz not null default now(),
  layer text not null check (layer in ('tactical', 'director')),
  thought text not null,
  say text not null,
  mood text not null check (mood in ('chill', 'bored', 'hyped', 'scared', 'smug')),
  goal text not null,
  action jsonb not null,
  confidence real not null check (confidence >= 0 and confidence <= 1),
  model text not null,
  input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  cached_tokens integer not null default 0,
  cost_usd numeric(12, 6) not null default 0
);
create index decisions_session_ts_idx on public.decisions (session_id, ts desc);

create table public.events (
  id bigint generated always as identity primary key,
  session_id uuid not null references public.sessions (id),
  ts timestamptz not null default now(),
  type text not null check (type in (
    'death', 'busted', 'mission_start', 'mission_end', 'mission_fail', 'wanted_change',
    'stunt', 'clip', 'break', 'governor_level', 'bridge_down', 'bridge_up', 'unstick',
    'activity_start', 'activity_end', 'session_start', 'session_end'
  )),
  payload jsonb not null default '{}'::jsonb,
  screenshot_url text
);
create index events_session_ts_idx on public.events (session_id, ts desc);
create index events_type_idx on public.events (type, ts desc);

create table public.missions (
  id bigint generated always as identity primary key,
  session_id uuid not null references public.sessions (id),
  name text not null,
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  outcome text check (outcome in ('passed', 'failed', 'skipped')), -- null while running
  attempts integer not null default 1,
  deaths integer not null default 0,
  tokens bigint not null default 0,
  summary text
);
create index missions_session_idx on public.missions (session_id, started_at desc);

create table public.clips (
  id bigint generated always as identity primary key,
  session_id uuid not null references public.sessions (id),
  ts timestamptz not null default now(),
  event_id bigint references public.events (id),
  storage_path text not null,
  duration_s real not null,
  caption text not null
);
create index clips_session_ts_idx on public.clips (session_id, ts desc);
create index clips_event_idx on public.clips (event_id);

create table public.stats (
  session_id uuid primary key references public.sessions (id),
  deaths integer not null default 0,
  busted integer not null default 0,
  missions_passed integer not null default 0,
  hours_alive real not null default 0,
  cost_today_usd numeric(12, 4) not null default 0,
  cost_per_hour_usd numeric(12, 4) not null default 0,
  governor_level smallint not null default 0 check (governor_level between 0 and 3),
  heartbeat_at timestamptz not null default now(),
  current_goal text not null default '',
  hud jsonb not null default '{}'::jsonb
);

create table public.site_config (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

-- Realtime: the site subscribes to inserts on decisions/events and updates on stats (D6).
-- The supabase_realtime publication exists on Supabase; guard for plain-Postgres test runs.
do $$
begin
  if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    create publication supabase_realtime;
  end if;
end
$$;
alter publication supabase_realtime add table public.decisions, public.events, public.stats;

-- Storage buckets (public read; writes are service-role only via the harness).
-- Screenshot retention (7 days) is enforced by the harness's daily cleanup via the storage API —
-- deleting storage.objects rows directly would orphan the underlying files.
do $$
begin
  if exists (select 1 from information_schema.tables
             where table_schema = 'storage' and table_name = 'buckets') then
    insert into storage.buckets (id, name, public)
    values ('clips', 'clips', true), ('shots', 'shots', true)
    on conflict (id) do nothing;
  end if;
end
$$;
