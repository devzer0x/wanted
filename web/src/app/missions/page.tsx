import type { Metadata } from "next";
import { fetchMissions, fetchStats } from "@/lib/data";
import { formatCompact, formatDuration, formatUtcStamp } from "@/lib/format";
import { routeMetadata } from "@/lib/metadata";
import type { MissionRow } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = routeMetadata({
  path: "/missions",
  title: "Missions",
  description: "Every story mission the agent has attempted, passed, or fumbled — live from the rig.",
});

const OUTCOME_STYLES: Record<string, string> = {
  passed: "border-bone text-bone",
  failed: "border-blood text-ember",
  skipped: "border-ash text-smoke",
};

function durationS(m: MissionRow): number | null {
  if (!m.ended_at) return null;
  const a = new Date(m.started_at).getTime();
  const b = new Date(m.ended_at).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(0, (b - a) / 1000);
}

function MissionCard({ m }: { m: MissionRow }) {
  const outcome = m.outcome ?? "in progress";
  return (
    <article className="panel flex flex-col gap-2 p-3 sm:p-4">
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-display text-base leading-tight text-bone [overflow-wrap:anywhere]">
          {m.name}
        </h3>
        <span
          className={`ticker shrink-0 border px-1.5 py-0.5 text-[0.58rem] ${OUTCOME_STYLES[outcome] ?? "border-hazard text-hazard"}`}
        >
          {outcome}
        </span>
      </div>
      <dl className="grid grid-cols-4 gap-1 font-mono text-[0.65rem] text-smoke">
        <div>
          <dt className="ticker text-[0.52rem]">Attempts</dt>
          <dd className="text-bone">{m.attempts ?? "—"}</dd>
        </div>
        <div>
          <dt className="ticker text-[0.52rem]">Deaths</dt>
          <dd className={m.deaths ? "text-ember" : "text-bone"}>{m.deaths ?? "—"}</dd>
        </div>
        <div>
          <dt className="ticker text-[0.52rem]">Time</dt>
          <dd className="text-bone">{formatDuration(durationS(m))}</dd>
        </div>
        <div>
          <dt className="ticker text-[0.52rem]">Tokens</dt>
          <dd className="text-bone">{formatCompact(m.tokens)}</dd>
        </div>
      </dl>
      {m.summary && (
        <p className="text-xs leading-relaxed text-smoke [overflow-wrap:anywhere]">{m.summary}</p>
      )}
      <p className="font-mono text-[0.55rem] text-ash">{formatUtcStamp(m.started_at)}</p>
    </article>
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
    <div className="flex flex-col gap-4 pt-4">
      <header className="flex flex-col gap-2">
        <h1 className="wordmark text-4xl sm:text-5xl" data-text="MISSIONS">
          MISSIONS
        </h1>
        <p className="max-w-2xl text-sm text-smoke">
          The story, as the agent stumbles through it. Progress below is built from the mission log —
          nothing is projected or padded.
        </p>
      </header>

      <section className="panel p-3 sm:p-4" aria-label="Overall story progress">
        <h2 className="panel-title mb-3">Story progress</h2>
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
          <div>
            <span className="font-display text-4xl text-bone">{statsPassed ?? passed}</span>
            <span className="ticker ml-2 text-[0.6rem] text-smoke">passed</span>
          </div>
          <div>
            <span className="font-display text-2xl text-ember">{failed}</span>
            <span className="ticker ml-2 text-[0.6rem] text-smoke">failed attempts logged</span>
          </div>
          <div>
            <span className="font-display text-2xl text-bone">{open}</span>
            <span className="ticker ml-2 text-[0.6rem] text-smoke">in progress</span>
          </div>
        </div>
        {rows.length > 0 && (
          <div
            className="mt-3 flex h-2 w-full gap-px overflow-hidden border border-ash bg-void"
            role="img"
            aria-label={`${passed} missions passed of ${rows.length} attempted`}
          >
            {rows
              .slice()
              .reverse()
              .map((m) => (
                <span
                  key={m.id}
                  className={
                    m.outcome === "passed"
                      ? "h-full flex-1 bg-bone"
                      : m.outcome === "failed"
                        ? "h-full flex-1 bg-blood"
                        : "h-full flex-1 bg-ash"
                  }
                />
              ))}
          </div>
        )}
      </section>

      {rows.length === 0 ? (
        <div className="panel px-4 py-12 text-center" data-testid="missions-empty">
          {missions.ok ? (
            <>
              <p className="wordmark text-2xl" data-text="NOTHING YET">
                NOTHING YET
              </p>
              <p className="mt-2 text-xs text-smoke">
                No missions on record. The story hasn&apos;t started.
              </p>
            </>
          ) : (
            <>
              <p className="wordmark text-2xl text-ember" data-text="DATA LINK DOWN">
                DATA LINK DOWN
              </p>
              <p className="mt-2 text-xs text-smoke">
                Mission log unavailable — the telemetry database is unreachable.
              </p>
            </>
          )}
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {rows.map((m) => (
            <MissionCard key={m.id} m={m} />
          ))}
        </div>
      )}
    </div>
  );
}
