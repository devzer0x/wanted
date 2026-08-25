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
    case "activity_end":
      return `Finished: ${str(p, "activity") ?? "an activity"}`;
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
