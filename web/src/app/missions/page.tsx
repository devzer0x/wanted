import type { Metadata } from "next";
import { fetchMissions, fetchStats } from "@/lib/data";
import { formatDuration, formatUtcStamp } from "@/lib/format";
import { routeMetadata } from "@/lib/metadata";
import type { MissionRow } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = routeMetadata({
  path: "/missions",
  title: "Missions",
  description: "Every story mission the agent has attempted, passed, or fumbled — live from the rig.",
});

// The four states a logged attempt can be in. `null` outcome is a mission still running, which the
// card and the track both draw yellow; "skipped" is in the frozen row contract, so it gets a
// neutral treatment rather than being lumped in with a failure.
type Outcome = "passed" | "failed" | "skipped" | "open";

function outcomeOf(m: MissionRow): Outcome {
  return m.outcome ?? "open";
}

const OUTCOME_LABEL: Record<Outcome, string> = {
  passed: "passed",
  failed: "failed",
  skipped: "skipped",
  open: "in progress",
};

/** Header strip of a mission card. The body stays white; only the strip carries the colour. */
const HEADER_STRIP: Record<Outcome, string> = {
  passed: "panel-teal",
  failed: "panel-coral text-white",
  skipped: "panel-sand",
  open: "panel-yellow",
};

const NODE_FILL: Record<Outcome, string> = {
  passed: "bg-teal",
  failed: "bg-coral",
  skipped: "bg-sand-deep",
  open: "bg-yellow",
};

const NODE_MARK: Record<Outcome, string> = {
  passed: "✓",
  failed: "×",
  skipped: "–",
  open: "…",
};

function durationS(m: MissionRow): number | null {
  if (!m.ended_at) return null;
  const a = new Date(m.started_at).getTime();
  const b = new Date(m.ended_at).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(0, (b - a) / 1000);
}

function StatBox({ value, label, tone }: { value: string; label: string; tone?: string }) {
  return (
    <div className="rounded-[10px] border-2 border-ink bg-sand px-2 py-1.5 text-center">
      <div className={`font-display text-[18px] leading-none ${tone ?? ""}`}>{value}</div>
      <div className="ticker mt-1 text-[9px] text-muted">{label}</div>
    </div>
  );
}

function MissionCard({ m }: { m: MissionRow }) {
  const outcome = outcomeOf(m);
  return (
    <article className="panel flex flex-col overflow-hidden">
      <div
        className={`flex items-center justify-between gap-2 border-b-[3px] border-ink px-3.5 py-2.5 ${HEADER_STRIP[outcome]}`}
      >
        <h3 className="font-display text-[18px] leading-tight [overflow-wrap:anywhere]">{m.name}</h3>
        {/* text-ink is explicit: on a coral (failed) strip the badge would otherwise
            inherit the strip's white text and vanish into its own white fill. */}
        <span className="pill pill-sm panel-title shrink-0 text-ink">{OUTCOME_LABEL[outcome]}</span>
      </div>
      <div className="flex flex-1 flex-col gap-2.5 p-3.5">
        <div className="grid grid-cols-3 gap-1.5">
          <StatBox value={m.attempts === null ? "—" : String(m.attempts)} label="tries" />
          <StatBox
            value={m.deaths === null ? "—" : String(m.deaths)}
            label="deaths"
            tone="text-coral"
          />
          <StatBox value={formatDuration(durationS(m))} label="time" />
        </div>
        {m.summary && (
          <p className="flex-1 text-[13px] leading-relaxed text-dim [overflow-wrap:anywhere]">
            {m.summary}
          </p>
        )}
        <p className="text-[11px] font-extrabold text-muted">{formatUtcStamp(m.started_at)}</p>
      </div>
    </article>
  );
}

/** One circular node per logged attempt, oldest on the left. */
function AttemptTrack({ rows, label }: { rows: MissionRow[]; label: string }) {
  // fetchMissions orders newest-first; the track reads in the order the attempts happened.
  const chronological = rows.slice().reverse();
  return (
    <div
      role="img"
      aria-label={label}
      className="flex items-center gap-1.5 overflow-x-auto py-1.5"
    >
      {chronological.map((m, i) => {
        const outcome = outcomeOf(m);
        return (
          <span key={m.id} className="flex flex-none items-center gap-1.5">
            <span
              className={`flex h-[30px] w-[30px] flex-none items-center justify-center rounded-full border-[3px] border-ink font-display text-[12px] leading-none shadow-[0_3px_0_var(--ink)] ${NODE_FILL[outcome]}`}
            >
              {NODE_MARK[outcome]}
            </span>
            {i < chronological.length - 1 && (
              <span aria-hidden="true" className="h-1 w-2.5 flex-none rounded-sm bg-ink" />
            )}
          </span>
        );
      })}
    </div>
  );
}

function EmptyState({
  title,
  body,
  tone,
}: {
  title: string;
  body: string;
  tone: "sand" | "coral";
}) {
  return (
    <>
      <p
        className={`inline-block rotate-[-1.5deg] rounded-[14px] border-[3px] border-ink px-5 py-2 font-display text-2xl uppercase shadow-[0_5px_0_var(--ink)] ${
          tone === "coral" ? "bg-coral text-white" : "bg-sand"
        }`}
      >
        {title}
      </p>
      <p className="mx-auto mt-4 max-w-sm text-sm leading-relaxed text-dim">{body}</p>
    </>
  );
}

export default async function MissionsPage() {
  const [missions, stats] = await Promise.all([fetchMissions(), fetchStats()]);
  const rows = missions.ok ? missions.rows : [];
  const passed = rows.filter((m) => m.outcome === "passed").length;
  const failed = rows.filter((m) => m.outcome === "failed").length;
  const open = rows.filter((m) => m.outcome === null).length;
  const statsPassed = stats.ok ? (stats.rows[0]?.missions_passed ?? null) : null;

  return (
    <div className="flex flex-col gap-5 pt-6">
      <header className="flex flex-col gap-3">
        <h1>
          <span className="page-title panel-teal">Story mode</span>
        </h1>
        <p className="max-w-xl text-[15px] leading-relaxed text-dim">
          The story, as the agent stumbles through it. Every node below is a real mission attempt
          from the log.
        </p>
      </header>

      {rows.length > 0 && (
        <section
          className="panel flex flex-col gap-3 p-4 sm:p-5"
          aria-label="Overall story progress"
        >
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-3">
            <div>
              <span className="font-display text-[44px] leading-none text-teal [-webkit-text-stroke:1.5px_var(--ink)]">
                {statsPassed ?? passed}
              </span>
              <span className="ticker ml-2 text-[12px] text-muted">passed</span>
            </div>
            <div>
              <span className="font-display text-[30px] leading-none text-coral [-webkit-text-stroke:1px_var(--ink)]">
                {failed}
              </span>
              <span className="ticker ml-2 text-[12px] text-muted">failed attempts</span>
            </div>
            <div>
              <span className="font-display text-[30px] leading-none text-yellow [-webkit-text-stroke:1px_var(--ink)]">
                {open}
              </span>
              <span className="ticker ml-2 text-[12px] text-muted">in progress</span>
            </div>
          </div>
          <AttemptTrack
            rows={rows}
            label={`${passed} missions passed of ${rows.length} attempted`}
          />
        </section>
      )}

      {rows.length === 0 ? (
        <div className="panel px-5 py-14 text-center" data-testid="missions-empty">
          {missions.ok ? (
            <EmptyState
              title="Nothing yet"
              body="No missions on record. The story hasn't started."
              tone="sand"
            />
          ) : (
            <EmptyState
              title="Data link down"
              body="Mission log unavailable — the telemetry database is unreachable."
              tone="coral"
            />
          )}
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {rows.map((m) => (
            <MissionCard key={m.id} m={m} />
          ))}
        </div>
      )}
    </div>
  );
}
