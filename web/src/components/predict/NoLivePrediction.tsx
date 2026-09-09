/**
 * The honest empty slot. There is no prediction open, so there is nothing to answer — this says
 * which of the two reasons it is and stops. It never renders a card shape with nothing behind it.
 */
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
        {sessionLive
          ? "No live prediction right now. The next one opens from something that happens on stream."
          : "The agent is off the air, so no prediction window is running."}
      </p>
    </section>
  );
}
