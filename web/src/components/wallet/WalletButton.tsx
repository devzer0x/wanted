"use client";

import { useEffect, useRef, useState } from "react";

import { RewardsPanel } from "@/components/wallet/RewardsPanel";
import { useWallet } from "@/components/wallet/WalletProvider";
import { shortAddress } from "@/lib/social";

/**
 * One button, every real wallet state: no extension installed, connecting, wrong network,
 * connected-but-not-signed-in, signed-in, and a declined request. Nothing here is a placeholder
 * — nobody clicks past a state that cannot really occur.
 *
 * The arcade chip: a hard 3px ink border and an offset shadow with no blur, so it reads as a
 * sticker pressed onto the header. Pressing it pushes the chip down onto its own shadow.
 */

/** Chip skins. The shadow colour is the only thing that changes per tone, so each is spelled out
 *  in full — Tailwind needs whole class names, not assembled ones. */
const TONES = {
  /** The default: the design's dark chip on a purple shadow. */
  ink:
    "border-ink bg-ink text-white shadow-[0_4px_0_var(--purple)] " +
    "hover:-translate-y-px hover:shadow-[0_5px_0_var(--purple)] " +
    "active:translate-y-[3px] active:shadow-[0_1px_0_var(--purple)]",
  /** Something needs the viewer's attention before predictions can work. */
  warn:
    "border-ink bg-yellow text-ink shadow-[0_4px_0_var(--ink)] " +
    "hover:-translate-y-px hover:shadow-[0_5px_0_var(--ink)] " +
    "active:translate-y-[3px] active:shadow-[0_1px_0_var(--ink)]",
  /** Nothing is wrong and nothing is possible — no extension to talk to. */
  plain:
    "border-ink bg-white text-ink shadow-[0_4px_0_var(--ink)] " +
    "hover:-translate-y-px hover:shadow-[0_5px_0_var(--ink)] " +
    "active:translate-y-[3px] active:shadow-[0_1px_0_var(--ink)]",
} as const;

// `.banner-throb` runs the pop with `animation-fill-mode: both`, whose backwards half paints the
// 0% keyframe — `opacity: 0`, `scale(0.7)` — whenever the animation has not actually advanced.
// Observed here: in a throttled tab the panel opened and stayed invisible at 70% size. Overriding
// the fill mode inline keeps the pop and makes the un-animated state the panel's real one, while
// the class's `prefers-reduced-motion` rule (`animation: none`) still wins outright.
const POP: React.CSSProperties = { animationFillMode: "none" };

export function WalletButton() {
  const wallet = useWallet();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const { chooserOpen, setChooserOpen } = wallet;
  useEffect(() => {
    if (!open && !chooserOpen) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setChooserOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open, chooserOpen, setChooserOpen]);

  // `caps` is false for an address on purpose. EIP-55 carries the checksum in letter case, so
  // running `text-transform: uppercase` over a hex address puts a DIFFERENT string on screen from
  // the one the wallet holds — the exact bug e2e/site.spec.ts records against the old contract
  // chip. Words take the design's caps; the address keeps its own case.
  const chip = (() => {
    if (wallet.status === "connecting") {
      return { label: "Connecting…", caps: true, tone: TONES.ink, dot: "var(--yellow)", busy: true };
    }
    if (wallet.status === "wrong-network") {
      return { label: "Wrong network", caps: true, tone: TONES.warn, dot: "var(--coral)", busy: false };
    }
    if (wallet.address) {
      return {
        label: shortAddress(wallet.address),
        caps: false,
        tone: TONES.ink,
        // Connected proves an address; only a verified session can predict. The dot says which.
        dot: wallet.signIn === "signed-in" ? "var(--teal)" : "var(--yellow)",
        busy: wallet.signIn === "signing-in",
      };
    }
    if (wallet.status === "no-wallet") {
      return { label: "No wallet found", caps: true, tone: TONES.plain, dot: "var(--sand-deep)", busy: false };
    }
    return { label: "Connect wallet", caps: true, tone: TONES.ink, dot: "var(--sand-deep)", busy: false };
  })();

  const onClick = () => {
    if (!wallet.address) {
      // A request is already open in the wallet; asking again is what earns a -32002.
      if (wallet.status === "connecting") return;
      // More than one wallet answered discovery: the viewer says which, rather than whichever
      // extension happened to announce first being prompted.
      if (wallet.providers.length > 1) {
        setChooserOpen(!chooserOpen);
        return;
      }
      wallet.beginConnect();
      return;
    }
    if (wallet.status === "wrong-network") {
      void wallet.switchNetwork();
      return;
    }
    setOpen((v) => !v);
  };

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={onClick}
        aria-haspopup={Boolean(wallet.address) || wallet.providers.length > 1}
        aria-expanded={open || chooserOpen}
        data-testid="wallet-button"
        data-wallet-status={wallet.status}
        className={`font-display inline-flex h-10 items-center gap-2 whitespace-nowrap rounded-xl border-[3px] px-3 text-[13px] leading-none tracking-[0.04em] transition-[transform,box-shadow] duration-75 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-coral sm:px-3.5 sm:text-sm ${
          chip.caps ? "uppercase" : ""
        } ${chip.tone}`}
      >
        <span
          aria-hidden="true"
          className={`h-[9px] w-[9px] flex-none rounded-full ${chip.busy ? "urgent" : ""}`}
          style={{ background: chip.dot, boxShadow: `0 0 0 2px var(--ink), 0 0 0 4px ${chip.dot}` }}
        />
        {chip.label}
      </button>

      {chooserOpen && !wallet.address && wallet.status !== "connecting" && wallet.providers.length > 1 && (
        <div
          role="dialog"
          aria-label="Choose a wallet"
          style={{ ...POP, transformOrigin: "top right" }}
          className="banner-throb absolute right-0 top-[calc(100%+12px)] z-70 w-[260px] max-w-[calc(100vw-24px)] rounded-[20px] border-[3px] border-ink bg-white p-3 text-ink shadow-[0_8px_0_var(--ink)]"
        >
          <p className="ticker px-1 pb-2 text-[10px] text-muted">Choose a wallet</p>
          <ul className="flex flex-col gap-2">
            {wallet.providers.map((detail) => (
              <li key={detail.info.uuid}>
                <button
                  type="button"
                  aria-label={detail.info.name}
                  onClick={() => {
                    setChooserOpen(false);
                    void wallet.connect(detail.info.uuid);
                  }}
                  className="flex w-full items-center gap-2.5 rounded-[14px] border-[3px] border-ink bg-white px-3 py-2 text-left text-[13px] font-black hover:bg-yellow-pale focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-coral"
                >
                  {/* EIP-6963 specifies `icon` as a data: URI. Anything else is not painted. */}
                  <span
                    aria-hidden="true"
                    className="h-6 w-6 flex-none rounded-md border-2 border-ink bg-sand bg-cover bg-center"
                    style={
                      detail.info.icon.startsWith("data:image/")
                        ? { backgroundImage: `url("${detail.info.icon}")` }
                        : undefined
                    }
                  />
                  <span className="min-w-0 truncate">{detail.info.name}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {wallet.status === "connecting" && !wallet.address && (
        <div
          data-testid="wallet-pending-hint"
          style={POP}
          className="panel banner-throb absolute right-0 top-[calc(100%+12px)] z-70 w-64 p-3 text-[11.5px] leading-[1.45] font-bold text-ink"
        >
          <p>
            Check your wallet and approve the connection. No popup? Open the wallet from the browser
            toolbar — it may be locked, or already showing a request.
          </p>
          <button
            type="button"
            onClick={() => wallet.cancelConnect()}
            className="mt-2 text-[12px] font-black text-coral underline underline-offset-2 hover:text-ink"
          >
            Cancel
          </button>
        </div>
      )}
      {!open && wallet.status === "no-wallet" && (
        <p
          style={POP}
          className="panel banner-throb absolute right-0 top-[calc(100%+12px)] z-70 w-60 p-3 text-[11.5px] leading-[1.45] font-bold text-muted"
        >
          No EIP-6963 wallet was found in this browser. Install one (e.g. a browser extension
          wallet) and reload to predict.
        </p>
      )}
      {!open && (wallet.status === "rejected" || wallet.status === "error") && wallet.errorMessage && (
        <p
          style={POP}
          className="banner-throb absolute right-0 top-[calc(100%+12px)] z-70 w-60 rounded-2xl border-[3px] border-ink bg-coral p-3 text-[11.5px] leading-[1.45] font-bold text-white shadow-[0_5px_0_var(--ink)]"
        >
          {wallet.errorMessage}
        </p>
      )}

      {open && wallet.address && (
        <div
          role="dialog"
          aria-label="Wallet"
          style={{ ...POP, transformOrigin: "top right" }}
          className="banner-throb absolute right-0 top-[calc(100%+12px)] z-70 w-[300px] max-w-[calc(100vw-24px)] rounded-[20px] border-[3px] border-ink bg-white p-4 text-ink shadow-[0_8px_0_var(--ink)]"
        >
          <div className="border-b-[3px] border-dashed border-sand-deep pb-3">
            <div className="flex items-center justify-between gap-2">
              <span className="ticker text-[10px] text-muted">Address</span>
              {/* Written out rather than built from `.pill`, whose white background is declared
                  outside Tailwind's layers and would beat any utility set on it here. */}
              <span className="inline-flex flex-none items-center rounded-full border-2 border-ink bg-purple px-2 py-[3px] text-[10px] leading-none font-extrabold tracking-[0.08em] text-white uppercase">
                Robinhood Chain
              </span>
            </div>
            {/* The whole address, in its own case — a truncated or re-cased one is a different
                string from the one the wallet holds. */}
            <p className="mt-1.5 break-all text-[12px] leading-[1.3] font-black">{wallet.address}</p>
          </div>

          {wallet.status === "wrong-network" ? (
            <div className="mt-3.5 flex flex-col gap-2.5">
              <p className="rounded-[14px] border-[3px] border-ink bg-yellow-pale px-3 py-2.5 text-[11.5px] leading-[1.45] font-bold">
                This wallet is on chain {wallet.chainId ?? "?"}, not Robinhood Chain (4663).
              </p>
              <button type="button" onClick={() => void wallet.switchNetwork()} className="btn btn-yellow w-full">
                Switch network
              </button>
              {wallet.errorMessage && (
                <p className="text-[11.5px] leading-[1.45] font-bold text-coral">{wallet.errorMessage}</p>
              )}
            </div>
          ) : wallet.signIn === "signed-in" ? (
            <RewardsPanel signedIn />
          ) : (
            <div className="mt-3.5 flex flex-col gap-2.5">
              <p className="text-[11.5px] leading-[1.45] font-bold text-muted">
                Sign the message in your wallet to predict and to see your balance.
              </p>
              <button
                type="button"
                onClick={() => void wallet.requestSignIn()}
                disabled={wallet.signIn === "signing-in"}
                className="btn btn-coral w-full"
              >
                {wallet.signIn === "signing-in" ? "Check your wallet…" : "Sign in to predict"}
              </button>
              {(wallet.signIn === "rejected" || wallet.signIn === "unavailable") && wallet.errorMessage && (
                <p className="text-[11.5px] leading-[1.45] font-bold text-coral">{wallet.errorMessage}</p>
              )}
            </div>
          )}

          <button
            type="button"
            onClick={() => void wallet.disconnect()}
            className="mt-3.5 text-[12px] font-black text-coral underline underline-offset-2 hover:text-ink"
          >
            Disconnect
          </button>
        </div>
      )}
    </div>
  );
}
