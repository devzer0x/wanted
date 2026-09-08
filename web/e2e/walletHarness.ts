// A REAL EIP-1193 provider, injected into the page the way a browser extension injects one, and
// announced over EIP-6963 exactly as `src/components/wallet/WalletProvider.tsx` discovers it.
//
// Why this exists: there is no extension wallet in a Playwright Chromium, so the whole connect →
// wrong-network → switch → sign-in path has never been executed against the real app. This file
// closes that gap without stubbing anything that belongs to us:
//
//   * The provider is the ONLY thing standing in for something we do not own (the extension).
//     Everything it does is what a wallet really does — it answers `eth_accounts` /
//     `eth_requestAccounts` / `eth_chainId`, it throws `{code: 4001}` when the user declines and
//     `{code: 4902}` for a chain it does not know, and it emits `accountsChanged` / `chainChanged`.
//   * `personal_sign` returns a REAL secp256k1 signature. The key is a fresh, never-funded
//     `generatePrivateKey()` account; signing happens in the Node test process through a Playwright
//     binding (`page.exposeFunction`) because the page has no viem of its own, and it signs the
//     RAW bytes the app handed to `personal_sign` — so the signature is over exactly the message
//     the server issued, and `verifyMessage` recovers the wallet's address from it.
//   * No route under `src/app/api/**` is intercepted, rewritten, or faked. Every request the app
//     makes during these tests goes to the real handler and gets whatever the real backend says.
//
// Verified against the installed versions rather than assumed (CLAUDE.md §6), viem 2.56.3:
//   requestAddresses -> `eth_requestAccounts`             (actions/wallet/requestAddresses.js)
//   getChainId       -> `eth_chainId`                     (actions/public/getChainId.js)
//   switchChain      -> `wallet_switchEthereumChain` with `[{chainId: numberToHex(id)}]`
//   addChain         -> `wallet_addEthereumChain`         (actions/wallet/addChain.js)
//   signMessage      -> `personal_sign` with `[stringToHex(message), address]`
//   buildRequest maps a thrown `{code: 4001}` to viem's `UserRejectedRequestError` (whose `.code`
//   is 4001) and `{code: 4902}` to `SwitchChainError` — which is what the app's `isUserRejection`
//   and its 4902 branch read.

import type { Page } from "@playwright/test";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

/** How the stand-in wallet behaves. Every field models something a real wallet really does. */
export interface HarnessBehavior {
  /** What `eth_chainId` answers, 0x-prefixed, exactly as an extension reports it. */
  chainIdHex: string;
  /** This site was authorised on an earlier visit, so `eth_accounts` answers without a prompt. */
  preAuthorized: boolean;
  /** The user declines the connection prompt (EIP-1193 code 4001). */
  rejectConnect: boolean;
  /** The user declines the signature prompt (EIP-1193 code 4001). */
  rejectSign: boolean;
  /** The user declines the network-switch prompt (EIP-1193 code 4001). */
  rejectSwitch: boolean;
  /** The wallet does not know this chain yet: switch throws 4902 until `wallet_addEthereumChain`. */
  chainUnknown: boolean;
}

const DEFAULT_BEHAVIOR: HarnessBehavior = {
  // Robinhood Chain mainnet. 4663 = 0x1237, per docs/CONTRACTS-PREDICTIONS.md §1 and
  // src/components/wallet/chain.ts (`WANTED_CHAIN_ID` 4663 / `WANTED_CHAIN_ID_HEX` "0x1237",
  // itself cross-checked against src/lib/chain/config.ts's DEFAULT_CHAIN_ID of 4663).
  chainIdHex: "0x1237",
  preAuthorized: false,
  rejectConnect: false,
  rejectSign: false,
  rejectSwitch: false,
  chainUnknown: false,
};

/** One JSON-RPC call the app made to the wallet, recorded in order. */
export interface RpcCall {
  method: string;
  params: unknown;
  /** What the wallet answered, when it answered (absent when the call threw). */
  result?: unknown;
}

interface HarnessInfo {
  uuid: string;
  name: string;
  icon: string;
  rdns: string;
}

interface HarnessConfig {
  address: string;
  info: HarnessInfo;
  behavior: HarnessBehavior;
}

/** The control surface the init script publishes on `window`, used only by the test process. */
interface HarnessControl {
  calls: () => RpcCall[];
  setBehavior: (patch: Partial<HarnessBehavior>) => void;
  emit: (event: string, payload: unknown) => void;
}

interface HarnessWindow extends Window {
  __walletHarness: HarnessControl;
  __walletHarnessSign: (messageHex: string) => Promise<string>;
}

export interface InstalledWallet {
  /** Checksummed address of the real local account backing the provider. */
  address: `0x${string}`;
  /** Every JSON-RPC call the app has made to the wallet so far, in order. */
  calls(): Promise<RpcCall[]>;
  /** Just the calls to one method. */
  callsTo(method: string): Promise<RpcCall[]>;
  /** Change how the wallet responds mid-test (e.g. the user declines the next prompt). */
  setBehavior(patch: Partial<HarnessBehavior>): Promise<void>;
  /** Fire an EIP-1193 event, the way a wallet does when the user changes chain/account in it. */
  emit(event: string, payload: unknown): Promise<void>;
}

// Runs in the page at document start, before any app code. Must be self-contained: Playwright
// serialises it, so it may not close over anything from this module.
function harnessInitScript(config: HarnessConfig): void {
  const w = window as unknown as HarnessWindow;
  const calls: RpcCall[] = [];
  const behavior: HarnessBehavior = Object.assign({}, config.behavior);
  const listeners = new Map<string, Array<(...args: unknown[]) => void>>();

  let accounts: string[] = behavior.preAuthorized ? [config.address] : [];
  let chainIdHex: string = behavior.chainIdHex;
  let chainKnown = !behavior.chainUnknown;

  function emit(event: string, payload: unknown): void {
    const handlers = listeners.get(event);
    if (!handlers) return;
    for (const handler of handlers.slice()) handler(payload);
  }

  // The shape a real injected wallet throws: a plain Error carrying an EIP-1193 `code`.
  function rpcError(code: number, message: string): Error {
    const err = new Error(message) as Error & { code: number };
    err.code = code;
    return err;
  }

  const provider = {
    // Some dapps sniff this; ours does not, but a real provider carries it.
    isWantedE2EHarness: true,
    async request(args: { method: string; params?: unknown }): Promise<unknown> {
      const method = args.method;
      const params = args.params === undefined ? null : args.params;
      // Recorded before dispatch so the order survives a call that throws (a declined prompt).
      const record: RpcCall = { method, params };
      calls.push(record);

      switch (method) {
        case "eth_accounts":
          return accounts;

        case "eth_requestAccounts": {
          if (behavior.rejectConnect) {
            throw rpcError(4001, "User rejected the request.");
          }
          accounts = [config.address];
          emit("accountsChanged", accounts);
          return accounts;
        }

        case "eth_chainId":
          return chainIdHex;

        case "net_version":
          return String(Number.parseInt(chainIdHex, 16));

        case "wallet_switchEthereumChain": {
          const list = params as Array<{ chainId?: string }> | null;
          const target = list && list[0] ? list[0].chainId : undefined;
          if (behavior.rejectSwitch) {
            throw rpcError(4001, "User rejected the request.");
          }
          if (!chainKnown) {
            throw rpcError(
              4902,
              "Unrecognized chain ID. Try adding the chain using wallet_addEthereumChain first."
            );
          }
          if (typeof target !== "string") {
            throw rpcError(-32602, "wallet_switchEthereumChain requires a chainId");
          }
          chainIdHex = target;
          emit("chainChanged", chainIdHex);
          return null;
        }

        case "wallet_addEthereumChain": {
          chainKnown = true;
          return null;
        }

        case "personal_sign": {
          if (behavior.rejectSign) {
            throw rpcError(4001, "User denied message signature.");
          }
          const list = params as [string, string] | null;
          if (!list || typeof list[0] !== "string") {
            throw rpcError(-32602, "personal_sign requires a message");
          }
          // Signed for real, in the Node process, over exactly these bytes.
          const signature = await w.__walletHarnessSign(list[0]);
          record.result = signature;
          return signature;
        }

        default:
          throw rpcError(4200, `Unsupported method: ${method}`);
      }
    },
    on(event: string, handler: (...args: unknown[]) => void): void {
      const existing = listeners.get(event);
      if (existing) existing.push(handler);
      else listeners.set(event, [handler]);
    },
    removeListener(event: string, handler: (...args: unknown[]) => void): void {
      const existing = listeners.get(event);
      if (!existing) return;
      const at = existing.indexOf(handler);
      if (at >= 0) existing.splice(at, 1);
    },
  };

  w.__walletHarness = {
    calls: () => calls.slice(),
    setBehavior: (patch: Partial<HarnessBehavior>) => {
      Object.assign(behavior, patch);
    },
    emit: (event: string, payload: unknown) => {
      if (event === "chainChanged" && typeof payload === "string") chainIdHex = payload;
      if (event === "accountsChanged" && Array.isArray(payload)) accounts = payload as string[];
      emit(event, payload);
    },
  };

  // EIP-6963: announce on request, and once immediately. The app dispatches
  // `eip6963:requestProvider` from a mount effect, which is what the listener below answers; the
  // eager announce covers a listener that was already attached. `detail` is frozen per the EIP.
  const detail = Object.freeze({ info: Object.freeze(config.info), provider });
  const announce = () => {
    window.dispatchEvent(new CustomEvent("eip6963:announceProvider", { detail }));
  };
  window.addEventListener("eip6963:requestProvider", announce);
  announce();
}

// A real (tiny) data URI, because EIP-6963's `info.icon` is specified as one. The app never
// renders it today; supplying a placeholder string instead would make this provider announce
// something no wallet would.
const HARNESS_ICON =
  "data:image/svg+xml;base64," +
  Buffer.from(
    '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#111"/></svg>'
  ).toString("base64");

/**
 * Installs the provider on `page`. MUST be called before `page.goto`, because a real extension is
 * present before the document runs and the app's EIP-6963 discovery happens on mount.
 *
 * Returns the address of the backing account plus the read/control handles the specs use. The
 * private key is generated per call and never leaves this process.
 */
export async function installWallet(
  page: Page,
  behavior: Partial<HarnessBehavior> = {}
): Promise<InstalledWallet> {
  const account = privateKeyToAccount(generatePrivateKey());

  // Real signing, in Node, over the raw bytes the app passed to `personal_sign` — never over a
  // string this harness reassembles, which is the whole point of the verbatim assertion.
  await page.exposeFunction("__walletHarnessSign", async (messageHex: string): Promise<string> => {
    return account.signMessage({ message: { raw: messageHex as `0x${string}` } });
  });

  const config: HarnessConfig = {
    address: account.address,
    info: {
      uuid: crypto.randomUUID(),
      name: "WANTED e2e harness wallet",
      icon: HARNESS_ICON,
      rdns: "dev.wanted.e2e-harness",
    },
    behavior: Object.assign({}, DEFAULT_BEHAVIOR, behavior),
  };

  await page.addInitScript(harnessInitScript, config);

  return {
    address: account.address,
    calls: () =>
      page.evaluate(() => (window as unknown as HarnessWindow).__walletHarness.calls()),
    callsTo: async (method: string) => {
      const all = await page.evaluate(() =>
        (window as unknown as HarnessWindow).__walletHarness.calls()
      );
      return all.filter((call) => call.method === method);
    },
    setBehavior: (patch: Partial<HarnessBehavior>) =>
      page.evaluate(
        (arg) => (window as unknown as HarnessWindow).__walletHarness.setBehavior(arg),
        patch
      ),
    emit: (event: string, payload: unknown) =>
      page.evaluate(
        (arg) => (window as unknown as HarnessWindow).__walletHarness.emit(arg.event, arg.payload),
        { event, payload }
      ),
  };
}
