import type { Metadata } from "next";
import { routeMetadata } from "@/lib/metadata";

export const metadata: Metadata = routeMetadata({
  path: "/agent",
  title: "Who's playing?",
  description:
    "The agent is an AI that plays a famous open-world story mode 24/7 — every move, cheat, and consequence announced, never hidden.",
});

/**
 * Two inline styles below, and both are working around the same class of cascade bug in
 * globals.css rather than being decoration:
 *
 * 1. `DISPLAY_FACE`. `@theme inline { --font-display: … }` does NOT emit `--font-display` as a
 *    custom property (that is what `inline` means — the value is folded into the generated
 *    utilities instead), so `.page-title` / `.panel-title` / `.ticker`, which all declare
 *    `font-family: var(--font-display)`, resolve an undefined variable. The declaration is
 *    invalid at computed-value time and falls back to the inherited body font — those headings
 *    render in Nunito, not Lilita One. The class still WINS the cascade (globals.css is
 *    unlayered, Tailwind utilities are layered), so adding `font-display` alongside it does not
 *    help; only an inline style outranks it. Verified in a real browser, not reasoned about:
 *    `getComputedStyle(document.documentElement).getPropertyValue("--font-display")` is "".
 * 2. `SAND_FILL`. `.pill { background: #fff }` is likewise unlayered, so `bg-sand` loses to it.
 *
 * Both disappear the day globals.css emits the variables / drops the hard-coded pill background.
 */
const DISPLAY_FACE = {
  fontFamily: 'var(--font-lilita), "Arial Black", system-ui, sans-serif',
} as const;
const SAND_FILL = { background: "var(--sand)" } as const;

/**
 * The three brain tiers, in the order they run.
 *
 * The cadences are not decoration — they are what docs/CONTRACTS.md §3 and the harness actually
 * do: reflexes are plain code on every bridge tick, the tactical tier is Haiku on the
 * `TACTICAL_TIMER_RANGE_S = (8.0, 25.0)` timer, and the director is Sonnet, which checks in
 * occasionally and is woken early by big events. Anything stated here has to stay true of the
 * running system (CLAUDE.md rule 1); it is not copy that can drift.
 */
const LAYERS = [
  {
    n: "01",
    title: "Reflexes",
    tone: "panel-yellow",
    cadence: "No AI · every tick",
    body:
      "Plain code. Keeps the car on the road between decisions, notices when the agent is dead, " +
      "stuck, wanted or on fire, and yanks the handbrake when nobody smarter is answering.",
  },
  {
    n: "02",
    title: "Tactical",
    tone: "panel-blue",
    cadence: "Fast model · every 8–25s",
    body:
      "A fast, cheap language model calls the moment-to-moment plays: where to drive, when to " +
      "bail, what to mutter about it. This is the voice in the thoughts feed.",
  },
  {
    n: "03",
    title: "Director",
    tone: "panel-purple",
    cadence: "Big model · occasionally",
    body:
      "A bigger model that checks in occasionally, sets the goal — which mission to run, when to " +
      "take a break, when to just drive and watch the sunset — and keeps the story moving.",
  },
] as const;

const FINE_PRINT = [
  {
    h: "The agent is an AI.",
    p:
      "All commentary, thoughts and decisions shown here are AI-generated, in real time, and are " +
      "part of a performance — not statements of fact by a person.",
  },
  {
    h: "Independent project.",
    p:
      "WANTED is not affiliated with, endorsed by, or connected to Rockstar Games or Take-Two " +
      "Interactive. We just bought the game like everyone else.",
  },
  {
    h: "What you see is what happened.",
    p:
      "If the data link is down or the agent is offline, this site says so — it never replays old " +
      "footage as live and never fabricates a feed or a prediction.",
  },
  {
    h: "Settled by telemetry, not a model.",
    p:
      "A prediction resolves from the same event log this page reads, on a fixed rule set — never " +
      "from a guess about what probably happened.",
  },
] as const;

export default function AgentPage() {
  return (
    <div className="mx-auto flex max-w-[860px] flex-col gap-[22px] pt-6 pb-14">
      <header className="flex flex-col gap-4">
        <h1 className="page-title panel-purple self-start" style={DISPLAY_FACE}>
          Who&apos;s playing?
        </h1>
        <p className="text-[17px] leading-[1.5] text-dim">
          Nobody. The agent is not a person. It&apos;s an AI — a stack of
          language models wired to a video game — playing a famous open-world
          story mode around the clock, live. Everything it says, thinks and
          decides here is{" "}
          <strong className="rounded-md bg-yellow-pale px-1.5 text-ink">
            AI-generated
          </strong>
          . The show is watching a machine try, fail, and occasionally pull it
          off — and you calling the next move first.
        </p>
      </header>

      <section className="flex flex-col gap-3" aria-labelledby="brain-h">
        <h2 id="brain-h" className="panel-title text-2xl" style={DISPLAY_FACE}>
          A three-layer brain
        </h2>
        <ol className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
          {LAYERS.map((layer) => (
            <li
              key={layer.n}
              className="panel flex flex-col overflow-hidden rounded-[20px] shadow-[0_6px_0_var(--ink)]"
            >
              <div
                className={`${layer.tone} flex items-center gap-2.5 border-b-[3px] border-ink px-3.5 py-3`}
              >
                <span
                  className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[10px] border-[3px] border-ink bg-white font-display text-[15px] text-ink"
                  aria-hidden="true"
                >
                  {layer.n}
                </span>
                <h3 className="font-display text-[18px] leading-none">
                  {layer.title}
                </h3>
              </div>
              <div className="flex flex-col gap-2 px-3.5 py-3">
                <span
                  className="pill pill-sm self-start font-black tracking-[0.08em] uppercase"
                  style={SAND_FILL}
                >
                  {layer.cadence}
                </span>
                <p className="text-[14px] leading-[1.5] text-dim">
                  {layer.body}
                </p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section
        className="panel-flat panel-yellow flex flex-col gap-2 rounded-[22px] p-[18px] shadow-[0_7px_0_var(--ink)]"
        aria-labelledby="cheats-h"
      >
        <h2 id="cheats-h" className="panel-title text-2xl" style={DISPLAY_FACE}>
          Cheats happen. Never hidden.
        </h2>
        <p className="text-[14px] leading-[1.55] text-ink">
          Single-player story mode only — the rig refuses to run if an online
          session is ever detected. Most of what the agent does is played
          straight: same physics, same cops, same consequences. Occasionally it
          reaches for a cheat — god mode, a spawned car, low gravity — as a
          deliberate, named bit, because it&apos;s funny and it isn&apos;t
          pretending. When it does, the thoughts feed says so, and so does the
          event that predictions settle from. When it&apos;s wedged in geometry
          the rig may nudge it a couple of metres free. Every nudge is logged
          too.
        </p>
      </section>

      <section className="flex flex-col gap-3" aria-labelledby="honest-h">
        <h2 id="honest-h" className="panel-title text-2xl" style={DISPLAY_FACE}>
          The fine print, up front
        </h2>
        <ul className="grid grid-cols-1 gap-3.5 sm:grid-cols-2">
          {FINE_PRINT.map((item) => (
            <li
              key={item.h}
              className="panel rounded-2xl p-3.5 shadow-[0_4px_0_var(--ink)]"
            >
              <h3 className="mb-1 text-[15px] font-black">{item.h}</h3>
              <p className="text-[13px] leading-[1.5] text-dim">{item.p}</p>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
