// Robinhood Chain connection config. Verified 2026-09-08 (docs/CONTRACTS-PREDICTIONS.md §1):
// chain id 4663 (0x1237) confirmed by direct `eth_chainId`, RPC and explorer confirmed against
// docs.robinhood.com/chain. These three facts are the ONLY chain values allowed a hardcoded
// default (they are public, non-secret and independently verified); everything else here is
// read from env with no invented fallback.
//
// NEXT_PUBLIC_* because a wallet connector (SIWE sign-in) needs the chain definition in the
// browser, and publicClient() is used for read-only calls that carry no secret.
//
//   NEXT_PUBLIC_CHAIN_ID            defaults to 4663 (Robinhood Chain mainnet)
//   NEXT_PUBLIC_CHAIN_RPC_URL       defaults to https://rpc.mainnet.chain.robinhood.com
//   NEXT_PUBLIC_CHAIN_EXPLORER_URL  defaults to https://robinhoodchain.blockscout.com
//
// Set all three together to point at testnet (chain id 46630, per the contract's testnet row)
// or any other Orbit deployment; a partial override that fails to parse falls back to the
// verified mainnet defaults and isChainConfigured() reports false so callers can degrade.

import { createPublicClient, defineChain, http, type Chain, type PublicClient } from "viem";

const DEFAULT_CHAIN_ID = 4663;
const DEFAULT_RPC_URL = "https://rpc.mainnet.chain.robinhood.com";
const DEFAULT_EXPLORER_URL = "https://robinhoodchain.blockscout.com";

interface ResolvedChainConfig {
  id: number;
  rpcUrl: string;
  explorerUrl: string;
}

function isValidUrl(value: string): boolean {
  try {
    new URL(value);
    return true;
  } catch {
    return false;
  }
}

/**
 * Resolves env overrides against the verified defaults. Returns null only when an explicitly
 * provided override is malformed (not a valid positive integer chain id, or not a parseable
 * URL) — a merely-absent value always falls through to the verified default and is valid.
 */
function resolveChainConfig(): ResolvedChainConfig | null {
  const idRaw = process.env.NEXT_PUBLIC_CHAIN_ID?.trim();
  const id = idRaw ? Number(idRaw) : DEFAULT_CHAIN_ID;
  if (!Number.isInteger(id) || id <= 0) return null;

  // Both spellings are accepted. NEXT_PUBLIC_RPC_URL / NEXT_PUBLIC_EXPLORER_URL are the names the
  // deployment brief and .env.example use; the NEXT_PUBLIC_CHAIN_* forms were this module's own and
  // are kept so an existing deployment does not silently lose its override. Reading only one of the
  // pair meant the documented variable did nothing: a deploy pointed at a paid RPC or at testnet
  // would have quietly kept using the default mainnet endpoint.
  const rpcUrl =
    process.env.NEXT_PUBLIC_RPC_URL?.trim() ||
    process.env.NEXT_PUBLIC_CHAIN_RPC_URL?.trim() ||
    DEFAULT_RPC_URL;
  if (!isValidUrl(rpcUrl)) return null;

  const explorerUrl =
    process.env.NEXT_PUBLIC_EXPLORER_URL?.trim() ||
    process.env.NEXT_PUBLIC_CHAIN_EXPLORER_URL?.trim() ||
    DEFAULT_EXPLORER_URL;
  if (!isValidUrl(explorerUrl)) return null;

  return { id, rpcUrl, explorerUrl };
}

const FALLBACK: ResolvedChainConfig = {
  id: DEFAULT_CHAIN_ID,
  rpcUrl: DEFAULT_RPC_URL,
  explorerUrl: DEFAULT_EXPLORER_URL,
};

const resolved = resolveChainConfig() ?? FALLBACK;

/**
 * Gas is ETH — Robinhood Chain is an Arbitrum Orbit EVM-equivalent chain with no separate native
 * gas token (docs/CONTRACTS-PREDICTIONS.md §1).
 */
export const ROBINHOOD_CHAIN: Chain = defineChain({
  id: resolved.id,
  name: "Robinhood Chain",
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: {
    default: { http: [resolved.rpcUrl] },
  },
  blockExplorers: {
    default: { name: "Blockscout", url: resolved.explorerUrl },
  },
});

let cachedClient: PublicClient | undefined;

export function publicClient(): PublicClient {
  if (!cachedClient) {
    cachedClient = createPublicClient({
      chain: ROBINHOOD_CHAIN,
      transport: http(),
    });
  }
  return cachedClient;
}

/** False when an explicit env override is malformed; a UI can then hide chain-dependent surfaces. */
export function isChainConfigured(): boolean {
  return resolveChainConfig() !== null;
}

function explorerBase(): string {
  return ROBINHOOD_CHAIN.blockExplorers?.default.url.replace(/\/+$/, "") ?? resolved.explorerUrl;
}

export function explorerTxUrl(hash: string): string {
  return `${explorerBase()}/tx/${hash}`;
}

export function explorerAddressUrl(a: string): string {
  return `${explorerBase()}/address/${a}`;
}
