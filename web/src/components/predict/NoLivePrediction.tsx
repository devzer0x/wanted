/**
 * The honest empty slot. There is no prediction open, so there is nothing to answer — this says
 * which of the two reasons it is and stops. It never renders a card shape with nothing behind it.
 *
 * The copy lives here and is exported because /predict renders its own empty slot with a different
 * layout and had its own copy of these sentences, which is how one of them came to describe a
 * lifecycle the harness no longer has.
 */

/** Live, but nothing open: since CONTRACTS-PREDICTIONS v2.4 a question is also asked on a timer
 *  (§3 "Cadence and entry windows", about every five minutes), not only off a dramatic moment.
 *  "Usually" rather than "every", because a scheduled round is skipped when no question's measured
 *  odds are fair — an empty slot is honest and a foregone conclusion is not. */
export const NOTHING_OPEN_LIVE =
  "No live prediction right now. Another usually opens within a few minutes while he is playing \u2014 sooner if something happens out there.";

export const NOTHING_OPEN_OFF_AIR = "The agent is off the air, so no prediction window is running.";
export function NoLivePrediction({ sessionLive }: { sessionLive: boolean }) {
  return (
    <section
      className="panel flex flex-col items-center justify-center gap-3 px-5 py-10 text-center lg:h-full"
      aria-label="Live prediction"
    >
      <span
        className="panel-title inline-block rounded-xl border-[3px] border-ink px-4 py-2 text-xl leading-none"
        style={{ background: "var(--sand)", boxShadow: "0 4px 0 var(--ink)", transform: "rotate(-2deg)" }}
      >
        Nothing open
      </span>
      <p className="max-w-xs text-[13px] font-bold leading-relaxed text-muted">
        {sessionLive ? NOTHING_OPEN_LIVE : NOTHING_OPEN_OFF_AIR}
      </p>
    </section>
  );
}
