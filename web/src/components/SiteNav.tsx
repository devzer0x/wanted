import Link from "next/link";

import { CopyContractButton } from "@/components/CopyContractButton";
import { Wordmark } from "@/components/Wordmark";
import { X_HANDLE, X_URL } from "@/lib/social";

const links = [
  { href: "/", label: "Live" },
  { href: "/missions", label: "Missions" },
  { href: "/clips", label: "Clips" },
  { href: "/agent", label: "the agent" },
];

export function SiteNav() {
  return (
    <header className="sticky top-0 z-60 border-b border-ash bg-void/92 backdrop-blur-sm">
      <div className="mx-auto flex min-h-14 w-full max-w-6xl flex-wrap items-center justify-between gap-x-3 gap-y-2 px-3 py-2 sm:px-5">
        <Link href="/" className="flex items-baseline gap-2" aria-label="WASTED home">
          <Wordmark className="text-2xl" />
        </Link>

        <nav aria-label="Main" className="order-3 w-full sm:order-none sm:w-auto">
          <ul className="flex items-center gap-1 sm:gap-2">
            {links.map((l) => (
              <li key={l.href}>
                <Link
                  href={l.href}
                  className="ticker px-2 py-2 text-[0.68rem] text-smoke transition-colors hover:text-ember focus-visible:text-ember sm:text-xs"
                >
                  {l.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>

        {/* Deliberately the heaviest things in the bar: these two are what people
            arrive looking for, and hunting for a 6px link is how they end up
            copying an address off somebody else's post instead. */}
        <div className="flex items-center gap-2">
          <a
            href={X_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`the agent on X, ${X_HANDLE} (opens in a new tab)`}
            className="ticker inline-flex h-10 items-center gap-2 border-2 border-ash bg-tar px-3 text-[0.7rem] font-bold text-bone transition-colors hover:border-ember hover:text-ember focus-visible:border-ember sm:px-4 sm:text-xs"
          >
            <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4 shrink-0" fill="currentColor">
              <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
            </svg>
            <span className="hidden sm:inline">{X_HANDLE}</span>
            <span className="sm:hidden">X</span>
          </a>

          <CopyContractButton />
        </div>
      </div>
      <div className="stripes-dim h-1" aria-hidden="true" />
    </header>
  );
}
