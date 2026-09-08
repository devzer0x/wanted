import Link from "next/link";

import { Wordmark } from "@/components/Wordmark";
import { WalletButton } from "@/components/wallet/WalletButton";

const links = [
  { href: "/", label: "Live" },
  { href: "/predict", label: "Predict" },
  { href: "/leaderboard", label: "Leaderboard" },
];

export function SiteNav() {
  return (
    <header className="sticky top-0 z-60 border-b border-ash bg-void/92 backdrop-blur-sm">
      <div className="mx-auto flex min-h-14 w-full max-w-6xl flex-wrap items-center justify-between gap-x-3 gap-y-2 px-3 py-2 sm:px-5">
        <Link href="/" className="flex items-baseline gap-2" aria-label="WANTED home">
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

        <WalletButton />
      </div>
      <div className="stripes-dim h-1" aria-hidden="true" />
    </header>
  );
}
