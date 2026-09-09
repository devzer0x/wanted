import type { Metadata } from "next";
import { LiveDashboard } from "@/components/live/LiveDashboard";
import { fetchDecisions, fetchEvents, fetchBorn, fetchStats, resolveStreamConfig } from "@/lib/data";
import { routeMetadata } from "@/lib/metadata";

// Live page: request-time render (no-store initial rows), then the client takes over via
// Supabase Realtime. Never ISR — the shell is cheap and the data must be honest.
export const dynamic = "force-dynamic";

// No `title`: the home page keeps the root layout's default title verbatim. The route group
// "(live)" does not appear in the URL, so the path here is "/".
export const metadata: Metadata = routeMetadata({ path: "/" });

export default async function LivePage() {
  const [decisions, events, stats, stream, born] = await Promise.all([
    fetchDecisions(),
    fetchEvents(),
    fetchStats(),
    resolveStreamConfig(),
    fetchBorn(),
  ]);

  const linkDown = !decisions.ok || !events.ok || !stats.ok;

  return (
    <>
      {/* The proposition, not the wordmark: the brand lives in the nav, so this heading says
          what the page IS. Deliberately not set as a sticker — CLAUDE.md §7 keeps the game's
          name out of anything that reads as our logo. */}
      <header className="flex flex-col gap-1 pt-4">
        <h1 className="font-display text-[1.55rem] leading-none sm:text-[2rem]">
          An AI plays GTA, live.
        </h1>
        <p className="max-w-lg text-sm text-dim">
          Predict what it does next — correct calls earn TTWO.
        </p>
      </header>
      <LiveDashboard
        initialDecisions={decisions.ok ? decisions.rows : []}
        initialEvents={events.ok ? events.rows : []}
        initialStats={stats.ok ? (stats.rows[0] ?? null) : null}
        bornAt={born.ok ? (born.rows[0]?.started_at ?? null) : null}
        stream={stream}
        initialLinkDown={linkDown}
        serverNowMs={Date.now()}
      />
    </>
  );
}
