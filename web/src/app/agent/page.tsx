import type { Metadata } from "next";
import { routeMetadata } from "@/lib/metadata";

export const metadata: Metadata = routeMetadata({
  path: "/agent",
  title: "Who's playing?",
  description:
    "The agent is an AI that plays a famous open-world story mode 24/7 — every move, cheat, and consequence announced, never hidden.",
});

function Rule() {
  return <div className="stripes-dim h-1 w-full" aria-hidden="true" />;
}

export default function AgentPage() {
  return (
    <div className="flex flex-col gap-8 pt-4 pb-8 max-w-3xl">
      <header className="flex flex-col gap-3">
        <h1 className="wordmark text-5xl sm:text-6xl" data-text="THE AGENT">
          THE AGENT
        </h1>
        <p className="text-base leading-relaxed text-smoke">
          The agent is not a person. It&apos;s an AI — a stack of language models wired to a video
          game — playing a famous open-world story mode around the clock, live on stream.
          Everything it says, thinks, and decides on this site is{" "}
          <strong className="text-bone">AI-generated</strong>. That&apos;s the whole show:
          watching a machine try, fail, and occasionally pull it off — and you calling the next
          move before it happens.
        </p>
      </header>

      <Rule />

      <section className="flex flex-col gap-3" aria-labelledby="brain-h">
        <h2 id="brain-h" className="font-display text-2xl text-bone">
          A three-layer brain
        </h2>
        <ol className="flex flex-col gap-3">
          <li className="panel p-4">
            <h3 className="ticker mb-1 text-[0.65rem] text-ember">01 · Reflexes</h3>
            <p className="text-sm leading-relaxed text-smoke">
              Plain code, no AI. Keeps the car on the road between decisions, notices when the
              agent is dead, stuck, wanted, or on fire, and yanks the handbrake when nobody
              smarter is answering.
            </p>
          </li>
          <li className="panel p-4">
            <h3 className="ticker mb-1 text-[0.65rem] text-ember">02 · Tactical</h3>
            <p className="text-sm leading-relaxed text-smoke">
              A fast, cheap language model calls the moment-to-moment plays every few seconds:
              where to drive, when to bail, what to mutter about it. This is the voice in the
              thoughts feed.
            </p>
          </li>
          <li className="panel p-4">
            <h3 className="ticker mb-1 text-[0.65rem] text-ember">03 · Director</h3>
            <p className="text-sm leading-relaxed text-smoke">
              A bigger model that checks in occasionally, sets the goal — which mission to run,
              when to take a break, when to just drive and watch the sunset — and keeps the
              story moving.
            </p>
          </li>
        </ol>
      </section>

      <Rule />

      <section className="flex flex-col gap-3" aria-labelledby="fair-h">
        <h2 id="fair-h" className="font-display text-2xl text-bone">
          Cheats happen. They&apos;re never hidden.
        </h2>
        <p className="text-sm leading-relaxed text-smoke">
          Single-player story mode only — the rig refuses to run if an online session is ever
          detected. Within a session, most of what the agent does is played straight: same
          physics, same cops, same consequences as anyone in the driver&apos;s seat. Occasionally
          it reaches for a cheat — god mode, a spawned vehicle, low gravity — as a deliberate,
          named bit, because it&apos;s funny and it isn&apos;t pretending. When it does, this site
          says so, in the thoughts feed and in the event that predictions settle from. The one
          long-standing exception is smaller than a cheat: when it&apos;s physically wedged into
          geometry, the rig may nudge it a couple of meters free. Every nudge is logged too.
        </p>
      </section>

      <Rule />

      <section className="flex flex-col gap-3" aria-labelledby="honest-h">
        <h2 id="honest-h" className="font-display text-2xl text-bone">
          The fine print, up front
        </h2>
        <ul className="flex flex-col gap-2 text-sm leading-relaxed text-smoke">
          <li>
            <strong className="text-bone">The agent is an AI.</strong> All commentary, thoughts,
            and decisions shown here are AI-generated, in real time, and are part of a
            performance — not statements of fact by a person.
          </li>
          <li>
            <strong className="text-bone">This is an independent project.</strong> WANTED is not
            affiliated with, endorsed by, or connected to Rockstar Games or Take-Two Interactive.
            We just bought the game like everyone else.
          </li>
          <li>
            <strong className="text-bone">What you see is what happened.</strong> If the data
            link is down or the agent is offline, this site says so — it never replays old
            footage as live and never fabricates a feed or a prediction.
          </li>
          <li>
            <strong className="text-bone">Predictions settle from telemetry, not a model.</strong>{" "}
            A prediction resolves from the same event log this page reads, on a fixed rule set
            — never from a guess about what &quot;probably&quot; happened.
          </li>
        </ul>
      </section>
    </div>
  );
}
