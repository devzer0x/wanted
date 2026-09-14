// The treasury key, and nothing else. docs/CONTRACTS-PREDICTIONS.md §10.4 — FROZEN INTERFACE.
//
// This module holds the only code in the repo that reads TREASURY_PRIVATE_KEY or builds a signed
// transaction. It has no route logic and no database access. Exactly one file may import it:
// web/src/lib/rewards/payoutWorker.ts, the lease-holding cron worker (§10.1 principle 1, "One
// signer"). No request path imports it — the claim route only enqueues.
//
// `import "server-only"` makes any client-component import a BUILD failure: Next.js aliases the
// bare "server-only" specifier to a module that throws when bundled for the browser, whether or not
// the npm package is installed (verified in next@15.5.23: node_modules/next/dist/build/
// create-compiler-aliases.js aliases `server-only$` on both compilation passes; TypeScript resolves
// the side-effect import through the ambient `declare module "server-only"` in
// node_modules/next/types/global.d.ts, referenced by next-env.d.ts).
//
//   TREASURY_PRIVATE_KEY   server-only secret, 32-byte hex (with or without 0x). Exists in Vercel
//                           Production ONLY (§10.7). Never NEXT_PUBLIC_, never logged, never echoed.
//   TREASURY_RPC_URL       server-only, optional. The RPC every treasury read and broadcast uses.
//                           Unset = the chain default from lib/chain/config (which honours
//                           NEXT_PUBLIC_RPC_URL). Present but malformed = treasury_not_configured,
//                           never a silent fall-back to the public endpoint.
//
// ERRORS (§10.4, principle 7): every error thrown from here is a TreasuryError whose message is
// exactly its code. No viem or @noble text is rethrown, attached as a `cause`, or logged: viem's
// messages carry the request body (which is the signed raw tx) and the RPC URL (which may embed a
// provider key), and @noble's scalar-range error prints the scalar itself — the private key (FM-17).
//
// NONCES (§10.1 principle 3): signTransfer takes the nonce as an argument, allocated by the
// database. Nothing here reads eth_getTransactionCount('pending') or uses viem's nonceManager, and
// signing touches no network at all; readLatestNonce reads 'latest' only for the worker's sanity
// checks, never to choose a nonce.

import "server-only";

import {
  BaseError,
  HttpRequestError,
  TimeoutError,
  TransactionReceiptNotFoundError,
  TransactionNotFoundError,
  createPublicClient,
  encodeFunctionData,
  http,
  isAddress,
  keccak256,
} from "viem";
import type { PublicClient } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import type { PrivateKeyAccount } from "viem/accounts";
import { ROBINHOOD_CHAIN } from "@/lib/chain/config";

export type TreasuryErrorCode =
  | "treasury_not_configured"
  | "treasury_key_invalid"
  | "rpc_unavailable"
  | "wrong_chain"
  | "token_paused"
  | "pause_unreadable"
  | "simulation_failed"
  | "broadcast_rejected";

/** The only error type this module throws. `message === code`; there is never a `cause`. */
export class TreasuryError extends Error {
  readonly code: TreasuryErrorCode;
  constructor(code: TreasuryErrorCode) {
    super(code);
    this.name = "TreasuryError";
    this.code = code;
  }
}

/** Robinhood Chain mainnet (CONTRACTS-PREDICTIONS §1, §10.2: chain_id "must be 4663"). The signed tx
 * carries this id, so bytes signed here cannot be replayed on any other chain. */
const PAYOUT_CHAIN_ID = 4663;

/** secp256k1 group order n (SEC 2 v2 §2.4.1). A private key is a scalar in [1, n). BigInt(string),
 * not a literal: tsconfig targets ES2017, which has no BigInt literal syntax. */
const SECP256K1_N = BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141");
const MAX_UINT256 = (BigInt(1) << BigInt(256)) - BigInt(1);
const ZERO = BigInt(0);
const ONE = BigInt(1);

const KEY_HEX_RE = /^0x[0-9a-f]{64}$/;
const HASH_RE = /^0x[0-9a-f]{64}$/;
const RAW_TX_RE = /^0x(?:[0-9a-f]{2})+$/;

/** Per-attempt RPC timeout and retry count for treasury READS. viem forces retryCount 0 on
 * eth_sendRawTransaction itself (actions/wallet/sendRawTransaction.js), so a broadcast is one
 * attempt. Worst case per read: 2 attempts x 6 s + 150 ms backoff — inside the worker's 8 s
 * SAFETY_MARGIN_MS cadence and far inside the cron's 60 s maxDuration. */
const RPC_TIMEOUT_MS = 6_000;
const RPC_READ_RETRIES = 1;

const ERC20_ABI = [
  {
    type: "function",
    name: "transfer",
    stateMutability: "nonpayable",
    inputs: [
      { name: "to", type: "address" },
      { name: "amount", type: "uint256" },
    ],
    outputs: [{ name: "", type: "bool" }],
  },
  {
    type: "function",
    name: "balanceOf",
    stateMutability: "view",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ name: "", type: "uint256" }],
  },
  {
    type: "function",
    name: "paused",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "bool" }],
  },
] as const;

// ---------------------------------------------------------------------------------------------
// The key
// ---------------------------------------------------------------------------------------------

type KeyState =
  | { kind: "absent" }
  | { kind: "invalid" }
  | { kind: "valid"; key: `0x${string}` };

/**
 * Reads and validates TREASURY_PRIVATE_KEY WITHOUT handing it to any library first: the scalar must
 * be in [1, n) before privateKeyToAccount ever sees it, because @noble/curves' own range error
 * prints the offending scalar (FM-17). Nothing here stringifies the key into a message.
 */
function readKey(): KeyState {
  const raw = process.env.TREASURY_PRIVATE_KEY?.trim();
  if (!raw) return { kind: "absent" };
  const hex = (/^0x/i.test(raw) ? raw.slice(2) : raw).toLowerCase();
  const normalized = `0x${hex}`;
  if (!KEY_HEX_RE.test(normalized)) return { kind: "invalid" };
  const scalar = BigInt(normalized);
  if (scalar < ONE || scalar >= SECP256K1_N) return { kind: "invalid" };
  return { kind: "valid", key: normalized as `0x${string}` };
}

/** Derived per call rather than cached: deriving costs one public-key multiplication, it happens a
 * handful of times per tick, and no second long-lived copy of the key sits in module state. */
function loadAccount(): PrivateKeyAccount {
  const state = readKey();
  if (state.kind === "absent") throw new TreasuryError("treasury_not_configured");
  if (state.kind === "invalid") throw new TreasuryError("treasury_key_invalid");
  try {
    return privateKeyToAccount(state.key);
  } catch {
    // Unreachable for an in-range scalar; if it ever fires, its text is not ours to repeat.
    throw new TreasuryError("treasury_key_invalid");
  }
}

/** True only when the key is present AND its scalar is in [1, n). */
export function isTreasuryConfigured(): boolean {
  return readKey().kind === "valid";
}

/** The treasury's address, lowercase; null when the key is absent or invalid. */
export function treasuryAddress(): `0x${string}` | null {
  try {
    return loadAccount().address.toLowerCase() as `0x${string}`;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------------------------
// The RPC
// ---------------------------------------------------------------------------------------------

function resolveRpcUrl(): string {
  const override = process.env.TREASURY_RPC_URL?.trim();
  if (override) {
    let parsed: URL;
    try {
      parsed = new URL(override);
    } catch {
      throw new TreasuryError("treasury_not_configured");
    }
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      throw new TreasuryError("treasury_not_configured");
    }
    return override;
  }
  const fallback = ROBINHOOD_CHAIN.rpcUrls.default.http[0];
  if (!fallback) throw new TreasuryError("treasury_not_configured");
  return fallback;
}

let cachedClient: { url: string; client: PublicClient } | null = null;

function rpc(): PublicClient {
  const url = resolveRpcUrl();
  if (cachedClient && cachedClient.url === url) return cachedClient.client;
  const client = createPublicClient({
    chain: ROBINHOOD_CHAIN,
    transport: http(url, { timeout: RPC_TIMEOUT_MS, retryCount: RPC_READ_RETRIES }),
  });
  cachedClient = { url, client };
  return client;
}

function isAbortLike(e: unknown): boolean {
  const name = (e as { name?: unknown } | null)?.name;
  return name === "AbortError" || name === "TimeoutError";
}

/** A failure to REACH the node (HTTP error, timeout, abort, socket error) as opposed to the node
 * answering with a JSON-RPC rejection. A non-viem throw is a fetch/socket failure by construction. */
function isTransportFailure(err: unknown): boolean {
  if (err instanceof BaseError) {
    return (
      err.walk(
        (e) => e instanceof HttpRequestError || e instanceof TimeoutError || isAbortLike(e),
      ) !== null
    );
  }
  return true;
}

/**
 * "This exact transaction is already in the pool or already mined" — which for a persisted raw tx
 * means the broadcast has done its job. Wording verified against the installed viem 2.56.3, not
 * guessed: `NonceTooLowError.nodeMessage` in node_modules/viem/_esm/errors/node.js is
 * /nonce too low|transaction already imported|already known/ (geth/Nitro say "already known",
 * reth/anvil "transaction already imported"); "already exists" covers nodes that phrase the same
 * pool condition that way. eth_sendRawTransaction errors are NOT run through viem's
 * getTransactionError (actions/wallet/sendRawTransaction.js calls client.request directly), so the
 * node's text survives as `details` on the RpcRequestError / HttpRequestError in the cause chain
 * (errors/base.js copies a BaseError cause's `details` up the chain).
 *
 * "nonce too low" can also mean ANOTHER tx consumed the nonce. That is still "known" here, by
 * contract (§10.4): the worker then reconciles by receipt, and a nonce consumed with no receipt for
 * any recorded hash goes to needs_review and halts — it is never read as success.
 */
const KNOWN_TX_RE = /already known|already imported|already exists|nonce too low/i;

function isKnownTransactionRejection(err: unknown): boolean {
  const texts: string[] = [];
  let cursor: unknown = err;
  for (let depth = 0; cursor && typeof cursor === "object" && depth < 8; depth += 1) {
    const node = cursor as { details?: unknown; message?: unknown; cause?: unknown };
    if (typeof node.details === "string") texts.push(node.details);
    // A BaseError's `message` embeds the request body (our raw tx) and the URL; only a plain
    // cause — the JSON-RPC error object the node returned — has a message that is the node's own.
    if (!(cursor instanceof BaseError) && typeof node.message === "string") texts.push(node.message);
    cursor = node.cause;
  }
  return texts.some((t) => KNOWN_TX_RE.test(t));
}

function assertAddress(value: string, code: TreasuryErrorCode): `0x${string}` {
  const lower = value.toLowerCase();
  if (!isAddress(lower)) throw new TreasuryError(code);
  return lower as `0x${string}`;
}

// ---------------------------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------------------------

/**
 * Everything the worker's preflight needs, read from the treasury RPC. Throws `wrong_chain` when
 * the RPC is not chain 4663 or this deployment's chain config is not 4663 — a tx signed for the
 * configured chain must never be judged against a different chain's state.
 */
export async function readChainState(token: `0x${string}`): Promise<{
  chainId: number;
  latestNonce: bigint;
  ethBalance: bigint;
  tokenBalance: bigint;
  paused: boolean;
  gasPrice: bigint;
}> {
  const tokenAddress = assertAddress(token, "treasury_not_configured");
  const address = loadAccount().address;
  const client = rpc();

  let chainId: number;
  let latestNonce: number;
  let ethBalance: bigint;
  let tokenBalance: bigint;
  let gasPrice: bigint;
  try {
    [chainId, latestNonce, ethBalance, tokenBalance, gasPrice] = await Promise.all([
      client.getChainId(),
      client.getTransactionCount({ address, blockTag: "latest" }),
      client.getBalance({ address, blockTag: "latest" }),
      client.readContract({
        address: tokenAddress,
        abi: ERC20_ABI,
        functionName: "balanceOf",
        args: [address],
      }),
      client.getGasPrice(),
    ]);
  } catch {
    throw new TreasuryError("rpc_unavailable");
  }
  if (chainId !== PAYOUT_CHAIN_ID || ROBINHOOD_CHAIN.id !== PAYOUT_CHAIN_ID) {
    throw new TreasuryError("wrong_chain");
  }

  let paused: boolean;
  try {
    paused = await client.readContract({ address: tokenAddress, abi: ERC20_ABI, functionName: "paused" });
  } catch {
    throw new TreasuryError("pause_unreadable");
  }

  return { chainId, latestNonce: BigInt(latestNonce), ethBalance, tokenBalance, paused, gasPrice };
}

/** eth_getTransactionCount(treasury, 'latest'). For sanity checks only — never to pick a nonce. */
export async function readLatestNonce(): Promise<bigint> {
  const address = loadAccount().address;
  // rpc() is resolved OUTSIDE the try, as in every other reader here: a malformed TREASURY_RPC_URL is
  // `treasury_not_configured` (a deployment fault), and the catch below would otherwise relabel it
  // `rpc_unavailable` (a node fault) — found by verify-treasury.mjs.
  const client = rpc();
  try {
    const n = await client.getTransactionCount({ address, blockTag: "latest" });
    return BigInt(n);
  } catch {
    throw new TreasuryError("rpc_unavailable");
  }
}

/**
 * Proves the transfer would succeed right now, from the treasury, and returns the node's gas
 * estimate. `transfer()` must return exactly `true` in simulation; a paused token is refused before
 * simulating (and an unreadable pause state is refused too — same fail-closed posture as §5).
 */
export async function simulateTransfer(p: {
  token: `0x${string}`;
  to: `0x${string}`;
  amountBase: bigint;
}): Promise<{ gasEstimate: bigint }> {
  const token = assertAddress(p.token, "simulation_failed");
  const to = assertAddress(p.to, "simulation_failed");
  if (p.amountBase <= ZERO || p.amountBase > MAX_UINT256) throw new TreasuryError("simulation_failed");

  const from = loadAccount().address;
  const client = rpc();

  let paused: boolean;
  try {
    paused = await client.readContract({ address: token, abi: ERC20_ABI, functionName: "paused" });
  } catch {
    throw new TreasuryError("pause_unreadable");
  }
  if (paused) throw new TreasuryError("token_paused");

  const call = {
    account: from,
    address: token,
    abi: ERC20_ABI,
    functionName: "transfer",
    args: [to, p.amountBase],
  } as const;

  let result: boolean;
  try {
    ({ result } = await client.simulateContract(call));
  } catch (err) {
    throw new TreasuryError(isTransportFailure(err) ? "rpc_unavailable" : "simulation_failed");
  }
  if (result !== true) throw new TreasuryError("simulation_failed");

  let gasEstimate: bigint;
  try {
    gasEstimate = await client.estimateContractGas(call);
  } catch (err) {
    throw new TreasuryError(isTransportFailure(err) ? "rpc_unavailable" : "simulation_failed");
  }
  if (gasEstimate <= ZERO) throw new TreasuryError("simulation_failed");
  return { gasEstimate };
}

// ---------------------------------------------------------------------------------------------
// Sign (no network) / broadcast / receipt
// ---------------------------------------------------------------------------------------------

/**
 * Signs `transfer(to, amountBase)` on `token` as an EIP-1559 tx for chain 4663 with value 0 and the
 * EXACT nonce, gas limit and fees given. Touches no network: the result is persisted by the worker
 * (record_signed_claim) before any node sees it (§10.1 principle 2). hash = keccak256(raw), computed
 * here — the same value a node returns for these bytes.
 *
 * Parameters are validated before the key is touched. There is no dedicated "invalid argument" code
 * in the frozen list; an unsignable request is reported as `simulation_failed` ("this transfer, as
 * requested, cannot proceed"), and a failure inside signing itself as `treasury_key_invalid`.
 */
export async function signTransfer(p: {
  token: `0x${string}`;
  to: `0x${string}`;
  amountBase: bigint;
  nonce: bigint;
  gasLimit: bigint;
  maxFeePerGas: bigint;
  maxPriorityFeePerGas: bigint;
}): Promise<{ raw: `0x${string}`; hash: `0x${string}` }> {
  const token = assertAddress(p.token, "simulation_failed");
  const to = assertAddress(p.to, "simulation_failed");
  if (
    p.amountBase <= ZERO ||
    p.amountBase > MAX_UINT256 ||
    p.nonce < ZERO ||
    p.nonce > BigInt(Number.MAX_SAFE_INTEGER) ||
    p.gasLimit <= ZERO ||
    p.maxFeePerGas <= ZERO ||
    p.maxFeePerGas > MAX_UINT256 ||
    p.maxPriorityFeePerGas < ZERO ||
    p.maxPriorityFeePerGas > p.maxFeePerGas
  ) {
    throw new TreasuryError("simulation_failed");
  }
  if (ROBINHOOD_CHAIN.id !== PAYOUT_CHAIN_ID) throw new TreasuryError("wrong_chain");

  const account = loadAccount();
  const data = encodeFunctionData({ abi: ERC20_ABI, functionName: "transfer", args: [to, p.amountBase] });

  let signed: `0x${string}`;
  try {
    signed = await account.signTransaction({
      type: "eip1559",
      chainId: PAYOUT_CHAIN_ID,
      nonce: Number(p.nonce),
      to: token,
      value: ZERO,
      data,
      gas: p.gasLimit,
      maxFeePerGas: p.maxFeePerGas,
      maxPriorityFeePerGas: p.maxPriorityFeePerGas,
    });
  } catch {
    throw new TreasuryError("treasury_key_invalid");
  }

  const raw = signed.toLowerCase() as `0x${string}`;
  if (!RAW_TX_RE.test(raw)) throw new TreasuryError("treasury_key_invalid");
  return { raw, hash: keccak256(raw) };
}

/**
 * eth_sendRawTransaction of already-persisted bytes. "accepted" = the node took it now; "known" =
 * the node says it already has it (pool or chain) — both mean the bytes are out. Anything else is
 * `rpc_unavailable` (could not reach the node; the broadcast may or may not have landed, which is
 * why the claim stays re-broadcastable) or `broadcast_rejected` (the node answered no). Neither is
 * ever grounds to fail a claim (§10.1 principle 4).
 */
export async function broadcastRaw(raw: `0x${string}`): Promise<"accepted" | "known"> {
  const bytes = raw.toLowerCase() as `0x${string}`;
  if (!RAW_TX_RE.test(bytes)) throw new TreasuryError("broadcast_rejected");
  const client = rpc();
  let returned: string;
  try {
    returned = await client.sendRawTransaction({ serializedTransaction: bytes });
  } catch (err) {
    if (isKnownTransactionRejection(err)) return "known";
    throw new TreasuryError(isTransportFailure(err) ? "rpc_unavailable" : "broadcast_rejected");
  }
  // A node that acknowledges different bytes than it was sent is not a node to trust with the
  // outcome; report it as unreachable so the claim stays signed and is re-broadcast next tick.
  if (typeof returned !== "string" || returned.toLowerCase() !== keccak256(bytes)) {
    throw new TreasuryError("rpc_unavailable");
  }
  return "accepted";
}

/** The receipt for `hash`, or null when the node has none yet (TransactionReceiptNotFoundError).
 * A null is "no receipt", never "failed" — only a status-0 receipt proves a revert. */
export async function getReceipt(
  hash: `0x${string}`,
): Promise<{ status: "success" | "reverted"; blockNumber: bigint } | null> {
  const wanted = hash.toLowerCase();
  // No "invalid argument" code exists in the frozen list; a malformed hash cannot be looked up, and
  // reporting it as unreachable keeps it far away from the "no receipt" branch.
  if (!HASH_RE.test(wanted)) throw new TreasuryError("rpc_unavailable");
  try {
    const receipt = await rpc().getTransactionReceipt({ hash: wanted as `0x${string}` });
    if (receipt.transactionHash.toLowerCase() !== wanted) throw new TreasuryError("rpc_unavailable");
    if (receipt.status !== "success" && receipt.status !== "reverted") {
      throw new TreasuryError("rpc_unavailable");
    }
    return { status: receipt.status, blockNumber: receipt.blockNumber };
  } catch (err) {
    if (err instanceof TreasuryError) throw err;
    if (err instanceof TransactionReceiptNotFoundError) return null;
    throw new TreasuryError("rpc_unavailable");
  }
}

/**
 * The sender and nonce of `hash`, or null when the node does not know the transaction
 * (TransactionNotFoundError). A receipt proves a transaction MINED; it does not say which account
 * sent it or which nonce it consumed. The worker uses this before honouring an operator-recorded
 * override (FS10, 2026-09-15 review): a `cancel` recorded with a transaction at the wrong nonce would
 * otherwise release a viewer's credits while the original signed transfer could still land.
 */
export async function getTransactionMeta(
  hash: `0x${string}`,
): Promise<{ from: `0x${string}`; nonce: bigint } | null> {
  const wanted = hash.toLowerCase();
  if (!HASH_RE.test(wanted)) throw new TreasuryError("rpc_unavailable");
  const client = rpc();
  try {
    const tx = await client.getTransaction({ hash: wanted as `0x${string}` });
    if (tx.hash.toLowerCase() !== wanted) throw new TreasuryError("rpc_unavailable");
    return { from: tx.from.toLowerCase() as `0x${string}`, nonce: BigInt(tx.nonce) };
  } catch (err) {
    if (err instanceof TreasuryError) throw err;
    if (err instanceof TransactionNotFoundError) return null;
    throw new TreasuryError("rpc_unavailable");
  }
}
