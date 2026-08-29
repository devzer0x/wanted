import type { Metadata } from "next";
import { LiveDashboard } from "@/components/live/LiveDashboard";
import {
  fetchDecisions,
  fetchEvents,
  fetchStats,
  fetchTokensToday,
  resolveStreamConfig,
} from "@/lib/data";
import { routeMetadata } from "@/lib/metadata";

// Live page: request-time render (no-store initial rows), then the client takes over via
// Supabase Realtime. Never ISR — the shell is cheap and the data must be honest.
export const dynamic = "force-dynamic";

// No `title`: the home page keeps the root layout's default title verbatim. The route group
// "(live)" does not appear in the URL, so the path here is "/".
export const metadata: Metadata = routeMetadata({ path: "/" });

export default async function LivePage() {
  const [decisions, events, stats, tokens, stream] = await Promise.all([
    fetchDecisions(),
    fetchEvents(),
    fetchStats(),
    fetchTokensToday(),
    resolveStreamConfig(),
  ]);

  const linkDown = !decisions.ok || !events.ok || !stats.ok;

  return (
    <>
      <h1 className="sr-only">WANTED — the agent plays live</h1>
      <LiveDashboard
        initialDecisions={decisions.ok ? decisions.rows : []}
        initialEvents={events.ok ? events.rows : []}
        initialStats={stats.ok ? (stats.rows[0] ?? null) : null}
        initialTokens={tokens}
        stream={stream}
        initialLinkDown={linkDown}
        serverNowMs={Date.now()}
      />
    </>
  );
}
