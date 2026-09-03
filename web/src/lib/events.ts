import type { EventRow } from "@/lib/types";

// Human lines for the CONTRACTS §4 event enum, derived strictly from row payload fields.
// Unknown types (a future contract version) fall back to the raw type name — shown, not hidden.

const str = (p: Record<string, unknown> | null, key: string): string | null => {
  const v = p?.[key];
  return typeof v === "string" && v.length > 0 ? v : null;
};
const num = (p: Record<string, unknown> | null, key: string): number | null => {
  const v = p?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
};

export function describeEvent(e: EventRow): string {
  const p = e.payload;
  switch (e.type) {
    case "death": {
      const street = str(p, "street");
      return `WASTED${street ? ` on ${street}` : ""}`;
    }
    case "busted": {
      const stars = num(p, "wanted_at_arrest");
      return `BUSTED${stars !== null ? ` at ${stars}★` : ""}`;
    }
    case "mission_start":
      return `Mission started: ${str(p, "name") ?? "unknown"}`;
    case "mission_end":
      return `Mission passed: ${str(p, "name") ?? "unknown"}`;
    case "mission_fail": {
      const reason = str(p, "reason_text");
      return `Mission failed: ${str(p, "name") ?? "unknown"}${reason ? ` — ${reason}` : ""}`;
    }
    case "wanted_change": {
      const from = num(p, "from");
      const to = num(p, "to");
      return from !== null && to !== null ? `Wanted ${from}★ → ${to}★` : "Wanted level changed";
    }
    case "stunt": {
      const airtime = num(p, "airtime_s");
      return `Stunt: ${str(p, "kind") ?? "jump"}${airtime !== null ? ` (${airtime.toFixed(1)}s air)` : ""}`;
    }
    case "clip":
      return "Clip saved";
    case "break": {
      const phase = str(p, "phase");
      return phase === "end" ? "Back from a break" : "Taking a break";
    }
    case "governor_level": {
      const to = num(p, "to");
      return `Budget governor → L${to ?? "?"}`;
    }
    case "bridge_down":
      return "Lost contact with the game";
    case "bridge_up":
      return "Game link restored";
    case "unstick": {
      const d = num(p, "distance_m");
      return `Unstick nudge${d !== null ? ` (${d.toFixed(1)} m)` : ""} — logged, no teleporting`;
    }
    case "activity_start":
      return `Started: ${str(p, "activity") ?? "an activity"}`;
    case "activity_end": {
      // The harness reports HOW a free-roam goal ended (`outcome`) and whether its own completion
      // test fired (`verified`). "Finished" was shown for every outcome — including a goal he never
      // moved for — which read as a lie on stream (operator, 2026-09-03: "said finished even when
      // he didn't"). Only a verified completion is "Finished".
      const activity = str(p, "activity") ?? "an activity";
      const outcome = str(p, "outcome");
      const verified = p?.["verified"] === true;
      if (outcome === "completed" || (outcome === null && verified)) return `Finished: ${activity}`;
      const label: Record<string, string> = {
        timeout: "Gave up on",
        stuck: "Stuck, dropped",
        preempted: "Dropped",
        wanted: "Cops cut short",
        mission: "Job cut short",
        player_down: "Cut short",
        actions_done_goal_unmet: "Didn't work out",
        escalate: "Didn't work out",
        bridge_task_lost: "Dropped",
        no_plan: "Dropped",
      };
      return `${outcome !== null && label[outcome] ? label[outcome] : "Ended"}: ${activity}`;
    }
    case "session_start":
      return "Session started";
    case "session_end":
      return "Session ended";
    default:
      return e.type.replace(/_/g, " ");
  }
}

/** Loud events get the red treatment in the feed. */
export function isLoudEvent(type: string): boolean {
  return type === "death" || type === "busted" || type === "mission_fail";
}
