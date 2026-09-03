"use client";

import { useCallback, useRef, useState } from "react";

import { CONTRACT_ADDRESS, shortAddress } from "@/lib/social";

/** The contract address as a big, obvious, one-tap copy button.
 *
 * The address is shown SHORTENED but always copied in FULL — a half-copied
 * token address is worse than none at all. The full string stays in the DOM for
 * screen readers and for anyone who wants to read it off the page rather than
 * trust the clipboard.
 */
export function CopyContractButton({ className = "" }: { className?: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(CONTRACT_ADDRESS);
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard is blocked on insecure contexts and by some mobile browsers.
      // A prompt pre-filled with the address is still a working copy path.
      window.prompt("Copy the contract address:", CONTRACT_ADDRESS);
    }
  }, []);

  return (
    <button
      type="button"
      onClick={copy}
      title={CONTRACT_ADDRESS}
      aria-label={`Copy contract address ${CONTRACT_ADDRESS}`}
      className={`ticker group inline-flex h-10 items-center gap-2 border-2 border-ash bg-tar px-3 text-[0.7rem] font-bold text-bone transition-colors hover:border-ember hover:text-ember focus-visible:border-ember sm:px-4 sm:text-xs ${className}`}
    >
      <span className="text-smoke transition-colors group-hover:text-ember">CA</span>
      {/* `normal-case` is load-bearing: `.ticker` uppercases, and a base58
          address is CASE-SENSITIVE. Rendering it uppercased would show a
          different address from the one the button copies, and somebody reading
          it off the screen instead of clicking would send funds nowhere. */}
      <span className="font-mono normal-case tracking-tight" aria-hidden="true">
        {copied ? "copied ✓" : shortAddress(CONTRACT_ADDRESS)}
      </span>
      <span className="sr-only">{CONTRACT_ADDRESS}</span>
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        className="h-3.5 w-3.5 shrink-0"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
      >
        <rect x="9" y="9" width="12" height="12" rx="1" />
        <path d="M5 15V4a1 1 0 0 1 1-1h9" />
      </svg>
    </button>
  );
}
