// The exact columns this site reads, shared by the server-side initial fetch (src/lib/data.ts)
// and the client-side incremental refetch (LiveDashboard) so the two can never drift.
//
// These are deliberate projections, not `select("*")`. The tables carry per-decision and
// per-session spend columns (`decisions.cost_usd`, `*_tokens`, `stats.cost_today_usd`,
// `stats.cost_per_hour_usd` — CONTRACTS §5). The public site does not show running costs, and
// selecting `*` would still ship those numbers into the browser inside the RSC payload where
// anyone could read them in view-source. Not fetching them is the only way the site actually
// stops publishing them. The columns remain in the database for the harness and its budget
// governor, which are unaffected.
//
// Every list below was verified to return HTTP 200 against the production Data API with the
// publishable key before it shipped.

/** Feed entry: mood chip, spoken line, expandable thought, layer label, timestamp. */
export const DECISION_COLUMNS = "id,ts,layer,thought,say,mood";

/** Feed entry: rendered by describeEvent() from `type` + `payload`, plus the screenshot link. */
export const EVENT_COLUMNS = "id,ts,type,payload,screenshot_url";

/** Counters, goal card, governor chip, HUD, and the heartbeat that drives ON/OFF. */
export const STATS_COLUMNS =
  "deaths,busted,missions_passed,hours_alive,governor_level,heartbeat_at,current_goal,hud";
