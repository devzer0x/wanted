import Link from "next/link";
import { Wordmark } from "@/components/Wordmark";

const links = [
  { href: "/", label: "Live" },
  { href: "/missions", label: "Missions" },
  { href: "/clips", label: "Clips" },
  { href: "/agent", label: "the agent" },
];

export function SiteNav() {
  return (
    <header className="sticky top-0 z-60 border-b border-ash bg-void/92 backdrop-blur-sm">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center justify-between gap-3 px-3 sm:px-5">
        <Link href="/" className="flex items-baseline gap-2" aria-label="WASTED home">
          <Wordmark className="text-2xl" />
        </Link>
        <nav aria-label="Main">
          <ul className="flex items-center gap-1 sm:gap-2">
            {links.map((l) => (
              <li key={l.href}>
                <Link
                  href={l.href}
                  className="ticker px-2 py-2 text-[0.68rem] sm:text-xs text-smoke transition-colors hover:text-ember focus-visible:text-ember"
                >
                  {l.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </div>
      <div className="stripes-dim h-1" aria-hidden="true" />
    </header>
  );
}
