import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Who is the agent?",
  description:
    "the agent is an AI that plays a famous open-world story mode 24/7 — no cheats, no god mode, full cost transparency.",
};

function Rule() {
  return <div className="stripes-dim h-1 w-full" aria-hidden="true" />;
}

export default function AgentPage() {
  return (
    <div className="flex flex-col gap-8 pt-4 pb-8 max-w-3xl">
      <header className="flex flex-col gap-3">
        <h1 className="wordmark text-5xl sm:text-6xl" data-text="WANTED">
          WANTED
        </h1>
        <p className="text-base leading-relaxed text-smoke">
          the agent is not a person. The agent is an AI — a stack of language models wired to a video
          game — playing a famous open-world story mode around the clock, live on stream.
          Everything he says, thinks, and decides on this site is{" "}
          <strong className="text-bone">AI-generated</strong>. That&apos;s the whole show:
          watching a machine try, fail, and occasionally pull it off.
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
              Plain code, no AI. Keeps the car on the road between decisions, notices when
              the agent is dead, stuck, wanted, or on fire, and yanks the handbrake when nobody
              smarter is answering.
            </p>
          </li>
          <li className="panel p-4">
            <h3 className="ticker mb-1 text-[0.65rem] text-ember">02 · Tactical</h3>
            <p className="text-sm leading-relaxed text-smoke">
              A fast, cheap language model calls the moment-to-moment plays every few seconds:
              where to drive, when to bail, what to mutter about it. This is the voice in the
              commentary feed.
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
          No cheats. Ever.
        </h2>
        <p className="text-sm leading-relaxed text-smoke">
          the agent plays with the same physics, the same cops, and the same consequences as any
          human in the driver&apos;s seat. No god mode, no teleports, no free money, no
          invincible cars. Single-player story mode only — the rig refuses to run if an online
          session is ever detected. The single exception: when he&apos;s physically wedged into
          geometry, the rig may nudge him a couple of meters free. Every nudge is logged and
          announced in the feed. If he dies, he dies. That&apos;s the counter on the front page.
        </p>
      </section>

      <Rule />

      <section className="flex flex-col gap-3" aria-labelledby="cost-h">
        <h2 id="cost-h" className="font-display text-2xl text-bone">
          The meter is running
        </h2>
        <p className="text-sm leading-relaxed text-smoke">
          Every one of the agent&apos;s thoughts is a paid API call. We publish the running cost —
          dollars per hour and tokens burned today — right on the{" "}
          <Link href="/" className="text-bone underline decoration-blood underline-offset-2 hover:text-ember">
            live page
          </Link>
          . When the hourly budget runs low, a governor throttles the brain: fewer thoughts,
          then reflexes only, then the agent parks somewhere scenic and sleeps it off. The site
          says so honestly when that happens.
        </p>
      </section>

      <Rule />

      <section className="flex flex-col gap-3" aria-labelledby="honest-h">
        <h2 id="honest-h" className="font-display text-2xl text-bone">
          The fine print, up front
        </h2>
        <ul className="flex flex-col gap-2 text-sm leading-relaxed text-smoke">
          <li>
            <strong className="text-bone">the agent is an AI.</strong> All commentary, thoughts, and
            decisions shown here are AI-generated, in real time, and are part of a performance —
            not statements of fact by a person.
          </li>
          <li>
            <strong className="text-bone">This is an independent project.</strong> WANTED is not
            affiliated with, endorsed by, or connected to Rockstar Games or Take-Two Interactive.
            We just bought the game like everyone else.
          </li>
          <li>
            <strong className="text-bone">What you see is what happened.</strong> If the data
            link is down or the agent is offline, this site says so — it never replays old footage
            as live and never fabricates a feed.
          </li>
        </ul>
      </section>
    </div>
  );
}
