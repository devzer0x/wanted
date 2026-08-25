import { LiveDashboard } from "@/components/live/LiveDashboard";
import {
  fetchDecisions,
  fetchEvents,
  fetchStats,
  fetchTokensToday,
  resolveStreamConfig,
} from "@/lib/data";

// Live page: request-time render (no-store initial rows), then the client takes over via
// Supabase Realtime. Never ISR — the shell is cheap and the data must be honest.
export const dynamic = "force-dynamic";

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
      />
    </>
  );
}
