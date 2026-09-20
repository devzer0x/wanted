"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { createWalletClient, custom, type WalletClient } from "viem";
import { apiErrorMessage } from "@/components/apiError";
import { WANTED_CHAIN_ID, wantedChain } from "@/components/wallet/chain";
import type {
  EIP1193Provider,
  EIP6963AnnounceProviderEvent,
  EIP6963ProviderDetail,
} from "@/components/wallet/types";

/**
 * Wallet connect + Sign-In-With-Ethereum, against the injected EIP-6963 provider directly
 * (no wagmi/RainbowKit/ConnectKit, per the brief). Every state below is something that can
 * really happen with a browser wallet — none is a placeholder for a state nobody has seen:
 *
 *  - "no-wallet": EIP-6963 discovery found nothing after asking.
 *  - "wrong-network": connected, but not on chain 4663 (CONTRACTS-PREDICTIONS §1).
 *  - "connecting": a connection request is open in the wallet. That includes one the wallet was
 *    ALREADY holding when the viewer clicked (EIP-1193 -32002) — a locked extension, a prompt
 *    behind another window, a request from an earlier tab. That is "look at your wallet", not a
 *    failure, so the state stays here and the page waits for the approval instead of painting
 *    the wallet's raw "Requested resource not available." in red, which is what it used to do.
 *  - "rejected": the account-connect or the sign-in request was declined in the wallet.
 *  - "error": the wallet or a fetch to the auth API failed for a real, surfaced reason.
 *
 * Connecting a wallet only proves an address; being able to PREDICT needs the server-verified
 * SIWE session (`signIn`), tracked separately because a viewer can hold one without the other
 * (e.g. reload with a live session cookie but no wallet re-attached yet).
 */

export type WalletStatus =
  | "idle"
  | "no-wallet"
  | "connecting"
  | "wrong-network"
  | "connected"
  | "rejected"
  | "error";

export type SignInStatus = "signed-out" | "signing-in" | "signed-in" | "rejected" | "unavailable";

interface WalletContextValue {
  providers: EIP6963ProviderDetail[];
  status: WalletStatus;
  address: `0x${string}` | null;
  chainId: number | null;
  errorMessage: string | null;
  signIn: SignInStatus;
  sessionEligible: boolean;
  connect: (uuid?: string) => Promise<void>;
  /**
   * What every "connect" button calls. With one wallet it connects; with several it opens the
   * header button's chooser instead, so no affordance on the page can prompt whichever extension
   * happened to announce first.
   */
  beginConnect: () => void;
  chooserOpen: boolean;
  setChooserOpen: (open: boolean) => void;
  /** Stop waiting on a connection request the viewer has given up on. */
  cancelConnect: () => void;
  disconnect: () => Promise<void>;
  switchNetwork: () => Promise<void>;
  requestSignIn: () => Promise<void>;
}

const WalletContext = createContext<WalletContextValue | null>(null);

function isUserRejection(err: unknown): boolean {
  const code = (err as { code?: number; cause?: { code?: number } })?.code;
  const causeCode = (err as { cause?: { code?: number } })?.cause?.code;
  return code === 4001 || causeCode === 4001;
}

/** EIP-1193 -32002 "resource unavailable": the wallet already has a request of this kind open. */
function isAlreadyPending(err: unknown): boolean {
  const code = (err as { code?: number })?.code;
  const causeCode = (err as { cause?: { code?: number } })?.cause?.code;
  return code === -32002 || causeCode === -32002;
}

/** How often, and for how long, to look for the approval of a request the wallet is holding. */
const PENDING_POLL_MS = 1_000;
const PENDING_WAIT_MS = 120_000;

/**
 * A wallet that predates EIP-6963 announces nothing and only sets `window.ethereum` (several
 * mobile in-app browsers still do). It is offered only when discovery found nothing, so a modern
 * wallet is never shadowed by whichever extension won the race for the global.
 */
function legacyProvider(): EIP6963ProviderDetail | null {
  const injected = (window as unknown as { ethereum?: EIP1193Provider }).ethereum;
  if (!injected || typeof injected.request !== "function") return null;
  return {
    info: { uuid: "window.ethereum", name: "Browser wallet", icon: "", rdns: "window.ethereum" },
    provider: injected,
  };
}

function readableError(err: unknown, fallback: string): string {
  const short = (err as { shortMessage?: unknown })?.shortMessage;
  if (typeof short === "string" && short) return short;
  if (err instanceof Error && err.message) return err.message.split("\n")[0].slice(0, 180);
  return fallback;
}

export function WalletProvider({ children }: { children: React.ReactNode }) {
  const [providers, setProviders] = useState<EIP6963ProviderDetail[]>([]);
  const [status, setStatus] = useState<WalletStatus>("idle");
  const [address, setAddress] = useState<`0x${string}` | null>(null);
  const [chainId, setChainId] = useState<number | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [signIn, setSignIn] = useState<SignInStatus>("signed-out");
  const [sessionEligible, setSessionEligible] = useState(false);
  const [chooserOpen, setChooserOpen] = useState(false);

  const providersRef = useRef<EIP6963ProviderDetail[]>([]);
  providersRef.current = providers;
  const activeProviderRef = useRef<EIP1193Provider | null>(null);
  const clientRef = useRef<WalletClient | null>(null);
  const triedSilentRef = useRef(false);
  // One connection request at a time: a second `eth_requestAccounts` while the first prompt is
  // open is exactly what makes a wallet answer -32002.
  const connectingRef = useRef(false);
  const pendingTimerRef = useRef<number | null>(null);

  const stopWaiting = useCallback(() => {
    if (pendingTimerRef.current !== null) window.clearTimeout(pendingTimerRef.current);
    pendingTimerRef.current = null;
    connectingRef.current = false;
  }, []);
  useEffect(() => stopWaiting, [stopWaiting]);

  // EIP-6963 discovery: listen for announcements, then ask every injected provider to announce.
  useEffect(() => {
    const onAnnounce = (event: Event) => {
      const detail = (event as EIP6963AnnounceProviderEvent).detail;
      if (!detail?.provider || !detail?.info) return;
      setProviders((prev) =>
        prev.some((p) => p.info.uuid === detail.info.uuid) ? prev : [...prev, detail]
      );
    };
    window.addEventListener("eip6963:announceProvider", onAnnounce as EventListener);
    window.dispatchEvent(new Event("eip6963:requestProvider"));
    // Announcements are synchronous replies to the request above, so anything that was going to
    // answer has. Only then is `window.ethereum` considered.
    const legacyTimer = window.setTimeout(() => {
      const legacy = legacyProvider();
      if (legacy) setProviders((prev) => (prev.length === 0 ? [legacy] : prev));
    }, 400);
    return () => {
      window.clearTimeout(legacyTimer);
      window.removeEventListener("eip6963:announceProvider", onAnnounce as EventListener);
    };
  }, []);

  const attachListeners = useCallback((provider: EIP1193Provider) => {
    const onAccountsChanged = (...args: unknown[]) => {
      const accounts = (args[0] as string[]) ?? [];
      if (accounts.length === 0) {
        setStatus("idle");
        setAddress(null);
        setSignIn("signed-out");
      } else {
        setAddress(accounts[0] as `0x${string}`);
        setSignIn("signed-out"); // a different account needs its own signature
      }
    };
    const onChainChanged = (...args: unknown[]) => {
      const id = Number.parseInt(args[0] as string, 16);
      setChainId(id);
      setStatus(id === WANTED_CHAIN_ID ? "connected" : "wrong-network");
    };
    provider.on?.("accountsChanged", onAccountsChanged);
    provider.on?.("chainChanged", onChainChanged);
  }, []);

  const attachClient = useCallback(
    (provider: EIP1193Provider, acct: `0x${string}`, id: number) => {
      activeProviderRef.current = provider;
      clientRef.current = createWalletClient({ chain: wantedChain, transport: custom(provider) });
      attachListeners(provider);
      setAddress(acct);
      setChainId(id);
      setStatus(id === WANTED_CHAIN_ID ? "connected" : "wrong-network");
    },
    [attachListeners]
  );

  // Silent reconnect: `eth_accounts` never prompts, so a wallet that already authorized this
  // site on an earlier visit re-attaches without a click. A wallet that has not is untouched —
  // this never calls `eth_requestAccounts`, which would pop a permission dialog unasked.
  useEffect(() => {
    if (triedSilentRef.current || providers.length === 0) return;
    triedSilentRef.current = true;
    void (async () => {
      for (const detail of providers) {
        try {
          const accounts = (await detail.provider.request({ method: "eth_accounts" })) as string[];
          if (accounts?.length) {
            const client = createWalletClient({ chain: wantedChain, transport: custom(detail.provider) });
            const id = await client.getChainId();
            attachClient(detail.provider, accounts[0] as `0x${string}`, id);
            return;
          }
        } catch {
          // Not every injected object answers eth_accounts before a real connect; that is fine,
          // the viewer just sees the ordinary "connect wallet" state.
        }
      }
    })();
  }, [providers, attachClient]);

  const refreshSession = useCallback(async () => {
    try {
      const res = await fetch("/api/auth/session", { credentials: "same-origin" });
      if (!res.ok) return;
      const body = (await res.json()) as { address: string | null; eligible: boolean };
      if (body.address) {
        setSignIn("signed-in");
        setSessionEligible(body.eligible);
      } else {
        setSignIn("signed-out");
        setSessionEligible(false);
      }
    } catch {
      // No session route reachable yet — the UI stays signed-out rather than guessing.
    }
  }, []);
  useEffect(() => {
    void refreshSession();
  }, [refreshSession]);

  // The wallet is holding a connection request this page cannot see or answer. `eth_accounts`
  // never prompts, so it is safe to ask repeatedly; it starts returning the account the moment
  // the viewer unlocks the wallet and approves.
  const waitForApproval = useCallback(
    (detail: EIP6963ProviderDetail) => {
      const startedAt = Date.now();
      const look = async () => {
        pendingTimerRef.current = null;
        try {
          const accounts = (await detail.provider.request({ method: "eth_accounts" })) as string[];
          if (!connectingRef.current) return; // cancelled while the wallet was answering
          if (accounts?.length) {
            const client = createWalletClient({ chain: wantedChain, transport: custom(detail.provider) });
            const id = await client.getChainId();
            stopWaiting();
            attachClient(detail.provider, accounts[0] as `0x${string}`, id);
            return;
          }
        } catch {
          // A locked wallet may refuse even eth_accounts; keep looking until the deadline.
        }
        if (!connectingRef.current) return;
        if (Date.now() - startedAt >= PENDING_WAIT_MS) {
          stopWaiting();
          setStatus("error");
          setErrorMessage(
            "Your wallet still has an unanswered request. Open the wallet, unlock it, approve or dismiss that request, then try again."
          );
          return;
        }
        pendingTimerRef.current = window.setTimeout(() => void look(), PENDING_POLL_MS);
      };
      pendingTimerRef.current = window.setTimeout(() => void look(), PENDING_POLL_MS);
    },
    [attachClient, stopWaiting]
  );

  const connect = useCallback(
    async (uuid?: string) => {
      if (connectingRef.current) return;
      connectingRef.current = true;
      setStatus("connecting");
      setErrorMessage(null);
      let pool = providersRef.current;
      if (pool.length === 0) {
        // Give the very first click a beat to collect eip6963:announceProvider replies.
        await new Promise((resolve) => setTimeout(resolve, 200));
        pool = providersRef.current;
      }
      if (pool.length === 0) {
        const legacy = legacyProvider();
        if (legacy) pool = [legacy];
      }
      const detail = uuid ? pool.find((p) => p.info.uuid === uuid) : pool[0];
      if (!detail) {
        connectingRef.current = false;
        setStatus("no-wallet");
        return;
      }
      const client = createWalletClient({ chain: wantedChain, transport: custom(detail.provider) });
      try {
        const [acct] = await client.requestAddresses();
        if (!connectingRef.current) return; // the viewer cancelled while the prompt was open
        const id = await client.getChainId();
        connectingRef.current = false;
        attachClient(detail.provider, acct, id);
      } catch (err) {
        if (!connectingRef.current) return;
        if (isAlreadyPending(err)) {
          waitForApproval(detail); // stays "connecting"; the button says where to look
          return;
        }
        connectingRef.current = false;
        if (isUserRejection(err)) {
          setStatus("rejected");
          setErrorMessage("Connection request was declined.");
        } else {
          setStatus("error");
          setErrorMessage(readableError(err, "Could not connect to the wallet."));
        }
      }
    },
    [attachClient, waitForApproval]
  );

  const beginConnect = useCallback(() => {
    if (connectingRef.current) return;
    if (providersRef.current.length > 1) {
      setChooserOpen(true);
      return;
    }
    void connect();
  }, [connect]);

  const cancelConnect = useCallback(() => {
    stopWaiting();
    setStatus("idle");
    setErrorMessage(null);
  }, [stopWaiting]);

  const switchNetwork = useCallback(async () => {
    const client = clientRef.current;
    if (!client) return;
    setErrorMessage(null);
    try {
      await client.switchChain({ id: WANTED_CHAIN_ID });
      setChainId(WANTED_CHAIN_ID);
      setStatus("connected");
      return;
    } catch (err) {
      const code = (err as { code?: number; cause?: { code?: number } })?.code ??
        (err as { cause?: { code?: number } })?.cause?.code;
      if (code === 4902) {
        try {
          await client.addChain({ chain: wantedChain });
          await client.switchChain({ id: WANTED_CHAIN_ID });
          setChainId(WANTED_CHAIN_ID);
          setStatus("connected");
          return;
        } catch (addErr) {
          setErrorMessage(readableError(addErr, "Could not add Robinhood Chain to the wallet."));
          return;
        }
      }
      if (isUserRejection(err)) {
        setErrorMessage("Network switch was declined.");
      } else {
        setErrorMessage(readableError(err, "Could not switch networks."));
      }
    }
  }, []);

  // One nonce+sign+verify pass. The message is never assembled client-side: /api/auth/nonce
  // returns the literal text to sign, built server-side from the stored nonce and this
  // deployment's pinned origin (src/app/api/_lib/siweDomain.ts). /verify rebuilds that same text
  // independently and checks the signature against ITS copy — signing anything else, even a
  // byte-identical-looking reconstruction, fails verification with no useful error. Returns
  // "retry" only for the one case that legitimately warrants requesting a fresh nonce and trying
  // again: a concurrent request burned this nonce first (409).
  const signInAttempt = useCallback(
    async (client: WalletClient, addr: `0x${string}`): Promise<"ok" | "retry"> => {
      const nonceRes = await fetch("/api/auth/nonce", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ address: addr }),
      });
      if (!nonceRes.ok) {
        throw new Error(await apiErrorMessage(nonceRes, "Sign-in is unavailable right now"));
      }
      const { message } = (await nonceRes.json()) as { message: string };
      const signature = await client.signMessage({ account: addr, message });
      const verifyRes = await fetch("/api/auth/verify", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ address: addr, signature }),
      });
      if (verifyRes.status === 409) return "retry";
      if (!verifyRes.ok) {
        throw new Error(await apiErrorMessage(verifyRes, "Sign-in could not be verified"));
      }
      return "ok";
    },
    []
  );

  const requestSignIn = useCallback(async () => {
    const client = clientRef.current;
    if (!client || !address) return;
    setSignIn("signing-in");
    setErrorMessage(null);
    try {
      let outcome = await signInAttempt(client, address);
      if (outcome === "retry") outcome = await signInAttempt(client, address);
      if (outcome === "retry") {
        throw new Error("Sign-in is unavailable right now — please try again.");
      }
      setSignIn("signed-in");
      await refreshSession();
    } catch (err) {
      if (isUserRejection(err)) {
        setSignIn("rejected");
        setErrorMessage("You declined the sign-in request.");
      } else {
        setSignIn("unavailable");
        setErrorMessage(readableError(err, "Sign-in is unavailable right now."));
      }
    }
  }, [address, refreshSession, signInAttempt]);

  const disconnect = useCallback(async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    } catch {
      // Best effort — local state clears regardless, so the UI never claims to still be signed
      // in after the viewer asked to disconnect.
    }
    stopWaiting();
    activeProviderRef.current = null;
    clientRef.current = null;
    setAddress(null);
    setChainId(null);
    setStatus("idle");
    setSignIn("signed-out");
    setSessionEligible(false);
    setErrorMessage(null);
  }, [stopWaiting]);

  return (
    <WalletContext.Provider
      value={{
        providers,
        status,
        address,
        chainId,
        errorMessage,
        signIn,
        sessionEligible,
        connect,
        beginConnect,
        chooserOpen,
        setChooserOpen,
        cancelConnect,
        disconnect,
        switchNetwork,
        requestSignIn,
      }}
    >
      {children}
    </WalletContext.Provider>
  );
}

export function useWallet(): WalletContextValue {
  const ctx = useContext(WalletContext);
  if (!ctx) throw new Error("useWallet must be used within a WalletProvider");
  return ctx;
}
