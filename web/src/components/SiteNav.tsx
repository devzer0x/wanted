"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

import { Wordmark } from "@/components/Wordmark";
import { WalletButton } from "@/components/wallet/WalletButton";

const links = [
  { href: "/", label: "Live" },
  { href: "/predict", label: "Predict" },
  { href: "/leaderboard", label: "Leaderboard" },
  { href: "/missions", label: "Missions" },
  { href: "/clips", label: "Clips" },
];

// "/" is only itself; every other tab also owns its detail pages (/clips/123 keeps CLIPS lit).
function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

// The colour utilities below carry `!` on purpose. globals.css styles `a` and `a:hover` outside any
// cascade layer, and unlayered rules beat every Tailwind utility (which live in @layer utilities)
// regardless of specificity — without the flag the ink/coral link colour wins over the pill's own.
export function SiteNav() {
  const pathname = usePathname();
  const listRef = useRef<HTMLUListElement>(null);

  // On a narrow screen the pill is wider than the viewport and scrolls sideways, so the tab you are
  // actually on can start off-screen (CLIPS, at the far right). Centre it inside the pill's own
  // scroller — never `scrollIntoView`, which would drag the whole page with it.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    if (list.scrollWidth <= list.clientWidth) return;
    const current = list.querySelector<HTMLElement>('[aria-current="page"]');
    if (!current) return;
    list.scrollLeft = Math.max(0, current.offsetLeft - (list.clientWidth - current.offsetWidth) / 2);
  }, [pathname]);

  return (
    <header className="sticky top-0 z-60 border-b-[3px] border-ink bg-cream/95 backdrop-blur-sm">
      <div className="mx-auto flex min-h-[68px] w-full max-w-6xl flex-wrap items-center justify-between gap-x-3 gap-y-2.5 px-3 py-2.5 sm:px-6">
        <Link href="/" className="inline-flex items-center gap-2.5" aria-label="WANTED home">
          {/* The mark carries its own ink border and offset shadow, so it needs no ring from the
              nav. Fixed box + `priority`: it is above the fold on every route, and letting it
              arrive late shifts the whole header. */}
          <Image
            src="/logo.png"
            alt=""
            width={512}
            height={512}
            priority
            className="h-8 w-8 shrink-0 sm:h-9 sm:w-9"
          />
          <Wordmark className="text-[22px] sm:text-[26px]" />
          <span className="hidden flex-col leading-[1.05] lg:flex">
            <span className="text-[12px] font-black tracking-[0.1em] uppercase">AI plays GTA</span>
            <span className="text-[11px] font-extrabold text-muted">Predict and Earn $TTWO</span>
          </span>
        </Link>

        <nav aria-label="Main" className="order-3 w-full sm:order-none sm:w-auto">
          <ul
            ref={listRef}
            className="flex list-none gap-1.5 overflow-x-auto rounded-full border-[3px] border-ink bg-white p-1 shadow-[0_4px_0_var(--ink)]"
          >
            {links.map((l) => {
              const active = isActive(pathname, l.href);
              return (
                <li key={l.href} className="flex-1">
                  <Link
                    href={l.href}
                    aria-current={active ? "page" : undefined}
                    className={`block rounded-full px-2.5 py-2 text-center font-display text-[12px] tracking-[0.04em] whitespace-nowrap uppercase transition-colors sm:px-3.5 sm:text-[15px] ${
                      active
                        ? "bg-ink text-cream! hover:text-cream!"
                        : "text-ink! hover:bg-yellow-pale hover:text-ink!"
                    }`}
                  >
                    {l.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <WalletButton />
      </div>
    </header>
  );
}
