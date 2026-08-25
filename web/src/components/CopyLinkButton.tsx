"use client";

import { useCallback, useRef, useState } from "react";

export function CopyLinkButton({ path }: { path: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const copy = useCallback(async () => {
    const url = `${window.location.origin}${path}`;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard can be blocked (permissions/insecure context); fall back to a prompt the
      // user can copy from manually.
      window.prompt("Copy this link:", url);
    }
  }, [path]);

  return (
    <button
      type="button"
      onClick={copy}
      className="ticker border border-ash px-2 py-1 text-[0.6rem] text-smoke transition-colors hover:border-blood hover:text-ember"
    >
      {copied ? "copied ✓" : "share link"}
    </button>
  );
}
