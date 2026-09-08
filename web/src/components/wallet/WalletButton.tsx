"use client";

import { useEffect, useRef, useState } from "react";
import { RewardsPanel } from "@/components/wallet/RewardsPanel";
import { useWallet } from "@/components/wallet/WalletProvider";
import { shortAddress } from "@/lib/social";

/**
 * One button, every real wallet state: no extension installed, connecting, wrong network,
 * connected-but-not-signed-in, signed-in, and a declined request. Nothing here is a placeholder
 * — nobody clicks past a state that cannot really occur.
 */
export function WalletButton() {
  const wallet = useWallet();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const label = (() => {
    if (wallet.status === "connecting") return "Connecting…";
    if (wallet.status === "wrong-network") return "Wrong network";
    if (wallet.address) return shortAddress(wallet.address);
    if (wallet.status === "no-wallet") return "No wallet found";
    if (wallet.status === "rejected") return "Connect wallet";
    return "Connect wallet";
  })();

  const live = wallet.status === "connected";

  const onClick = () => {
    if (!wallet.address) {
      void wallet.connect();
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
        aria-haspopup={Boolean(wallet.address)}
        aria-expanded={open}
        data-testid="wallet-button"
        data-wallet-status={wallet.status}
        className={`ticker inline-flex h-10 items-center gap-2 border-2 px-3 text-[0.68rem] font-bold transition-colors sm:px-4 ${
          wallet.status === "wrong-network"
            ? "border-hazard text-hazard hover:bg-hazard hover:text-void"
            : "border-ash bg-tar text-bone hover:border-ember hover:text-ember"
        }`}
      >
        <span className={`led ${live ? "led-live" : "led-dead"}`} aria-hidden="true" />
        {label}
      </button>

      {!open && wallet.status === "no-wallet" && (
        <p className="absolute right-0 top-full z-70 mt-1 w-56 border border-ash bg-tar p-2 text-[0.62rem] leading-relaxed text-smoke">
          No EIP-6963 wallet was found in this browser. Install one (e.g. a browser extension
          wallet) and reload to predict.
        </p>
      )}
      {!open && wallet.status === "rejected" && wallet.errorMessage && (
        <p className="absolute right-0 top-full z-70 mt-1 w-56 border border-ash bg-tar p-2 text-[0.62rem] leading-relaxed text-ember">
          {wallet.errorMessage}
        </p>
      )}
      {!open && wallet.status === "error" && wallet.errorMessage && (
        <p className="absolute right-0 top-full z-70 mt-1 w-56 border border-ash bg-tar p-2 text-[0.62rem] leading-relaxed text-ember">
          {wallet.errorMessage}
        </p>
      )}

      {open && wallet.address && (
        <div
          role="dialog"
          aria-label="Wallet"
          className="panel absolute right-0 top-full z-70 mt-1 w-72 p-3"
        >
          <div className="mb-2 flex items-center justify-between gap-2 border-b border-ash pb-2">
            <span className="font-mono text-[0.68rem] text-bone" title={wallet.address}>
              {shortAddress(wallet.address, 8, 6)}
            </span>
            <span className="ticker text-[0.55rem] text-smoke">Robinhood Chain</span>
          </div>

          {wallet.status === "wrong-network" ? (
            <div className="flex flex-col gap-2">
              <p className="text-[0.65rem] leading-relaxed text-hazard">
                This wallet is on chain {wallet.chainId ?? "?"}, not Robinhood Chain (4663).
              </p>
              <button
                type="button"
                onClick={() => void wallet.switchNetwork()}
                className="ticker border border-hazard px-2 py-1.5 text-[0.62rem] text-hazard hover:bg-hazard hover:text-void"
              >
                Switch network
              </button>
              {wallet.errorMessage && <p className="text-[0.6rem] text-ember">{wallet.errorMessage}</p>}
            </div>
          ) : wallet.signIn === "signed-in" ? (
            <RewardsPanel signedIn />
          ) : (
            <div className="flex flex-col gap-2">
              <p className="text-[0.65rem] leading-relaxed text-smoke">
                Sign the message in your wallet to predict and to see your balance.
              </p>
              <button
                type="button"
                onClick={() => void wallet.requestSignIn()}
                disabled={wallet.signIn === "signing-in"}
                className="ticker border border-blood px-2 py-1.5 text-[0.62rem] text-ember transition-colors hover:bg-blood hover:text-bone disabled:opacity-60"
              >
                {wallet.signIn === "signing-in" ? "Check your wallet…" : "Sign in to predict"}
              </button>
              {(wallet.signIn === "rejected" || wallet.signIn === "unavailable") &&
                wallet.errorMessage && <p className="text-[0.6rem] text-ember">{wallet.errorMessage}</p>}
            </div>
          )}

          <button
            type="button"
            onClick={() => void wallet.disconnect()}
            className="ticker mt-3 w-full border-t border-ash pt-2 text-left text-[0.6rem] text-smoke hover:text-ember"
          >
            Disconnect
          </button>
        </div>
      )}
    </div>
  );
}
