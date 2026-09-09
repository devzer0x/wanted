import Link from "next/link";

import { Wordmark } from "@/components/Wordmark";

const links = [
  { href: "/predict", label: "Predict" },
  { href: "/leaderboard", label: "Leaderboard" },
  { href: "/missions", label: "Missions" },
  { href: "/clips", label: "Clips" },
  { href: "/agent", label: "Who's playing?" },
];

// The `!` on the link colours is required: globals.css styles `a` and `a:hover` outside any cascade
// layer, and unlayered rules outrank every Tailwind utility whatever the specificity.
export function SiteFooter() {
  return (
    <footer className="border-t-[3px] border-ink bg-ink-deep">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 px-3 py-7 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Wordmark className="text-[22px]" tone="light" />
          <div className="flex flex-wrap gap-1.5">
            {links.map((l) => (
              <Link
                key={l.href}
                href={l.href}
                className="rounded-full border-2 border-cream/35 px-3 py-[5px] font-display text-[13px] tracking-[0.06em] whitespace-nowrap text-cream! uppercase transition-colors hover:border-cream hover:bg-cream hover:text-ink!"
              >
                {l.label}
              </Link>
            ))}
          </div>
        </div>

        <p className="max-w-[720px] text-xs leading-[1.55] font-bold text-purple-pale/80">
          WANTED is an independent art-and-engineering experiment. It is not affiliated with,
          endorsed by, or connected to Rockstar Games or Take-Two Interactive. The agent is an
          AI; all commentary and gameplay decisions on this site are AI-generated.
        </p>

        <span className="text-[11px] font-extrabold tracking-[0.1em] text-purple-pale/60 uppercase">
          Single-player story mode only · cheats are announced, never hidden
        </span>
      </div>
    </footer>
  );
}
