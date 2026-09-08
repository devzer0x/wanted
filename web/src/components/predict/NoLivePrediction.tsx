export function NoLivePrediction({ sessionLive }: { sessionLive: boolean }) {
  return (
    <section
      className="panel flex flex-col items-center justify-center gap-2 px-4 py-8 text-center lg:h-full"
      aria-label="Live prediction"
    >
      <p className="wordmark text-xl" data-text="NOTHING OPEN">
        NOTHING OPEN
      </p>
      <p className="max-w-xs text-xs leading-relaxed text-smoke">
        {sessionLive
          ? "No live prediction right now. The next one opens from something that happens on stream."
          : "The agent is off the air, so no prediction window is running."}
      </p>
    </section>
  );
}
