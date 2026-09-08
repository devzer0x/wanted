// The treasury wallet. This is the only file in the repo permitted to construct a wallet client
// or touch TREASURY_PRIVATE_KEY. `import "server-only"` makes any accidental client-component
// import a BUILD failure — Next.js aliases the bare "server-only" specifier to a module that
// throws when bundled for the browser, whether or not the separate npm package is installed
// (verified in the installed next@15.5.23: node_modules/next/dist/build/create-compiler-aliases.js
// aliases `server-only$` to next's own compiled copy on both the server and client compilation
// passes; TypeScript resolves the side-effect import via the ambient `declare module "server-only"`
// shipped in node_modules/next/types/global.d.ts, referenced by this project's next-env.d.ts).
//
//   TREASURY_PRIVATE_KEY   server-only secret, 32-byte hex (with or without 0x prefix). Never
//                           read via NEXT_PUBLIC_, never logged, never echoed in an error.

import "server-only";

import { createWalletClient, http, isAddress } from "viem";
import { privateKeyToAccount, type PrivateKeyAccount } from "viem/accounts";
import { ROBINHOOD_CHAIN } from "@/lib/chain/config";
import { isTokenPaused, rewardAsset } from "@/lib/chain/assets";

const ERC20_TRANSFER_ABI = [
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
] as const;

const PRIVATE_KEY_RE = /^0x[0-9a-fA-F]{64}$/;

/** Never returns the raw value in a form suitable for logging by accident — callers get a key
 * or nothing, and nothing here ever stringifies the return value into a message. */
function readTreasuryPrivateKey(): `0x${string}` | null {
  const raw = process.env.TREASURY_PRIVATE_KEY?.trim();
  if (!raw) return null;
  const normalized = raw.startsWith("0x") ? raw : `0x${raw}`;
  return PRIVATE_KEY_RE.test(normalized) ? (normalized as `0x${string}`) : null;
}

export function isTreasuryConfigured(): boolean {
  return readTreasuryPrivateKey() !== null;
}

let cachedAccount: PrivateKeyAccount | null | undefined;

function treasuryAccount(): PrivateKeyAccount | null {
  if (cachedAccount !== undefined) return cachedAccount;
  const key = readTreasuryPrivateKey();
  cachedAccount = key ? privateKeyToAccount(key) : null;
  return cachedAccount;
}

export async function sendReward(p: {
  to: `0x${string}`;
  amount: bigint;
}): Promise<{ hash: `0x${string}` }> {
  if (!isAddress(p.to)) {
    throw new Error("sendReward: recipient is not a valid address");
  }
  if (p.amount <= BigInt(0)) {
    throw new Error("sendReward: amount must be a positive number of base units");
  }

  const account = treasuryAccount();
  if (!account) {
    throw new Error("sendReward: treasury is not configured");
  }

  const asset = rewardAsset();
  if (!asset) {
    throw new Error("sendReward: reward asset is not configured");
  }

  let paused: boolean;
  try {
    paused = await isTokenPaused(asset.address);
  } catch (err) {
    // Cannot confirm the token is unpaused — refuse rather than guess, same safe-default
    // posture as policy eligibility (§5).
    const reason = err instanceof Error ? err.message : "unknown error";
    throw new Error(`sendReward: could not confirm reward token pause state, refusing to send (${reason})`);
  }
  if (paused) {
    throw new Error("sendReward: reward token is paused, refusing to send");
  }

  const wallet = createWalletClient({
    account,
    chain: ROBINHOOD_CHAIN,
    transport: http(),
  });

  const hash = await wallet.writeContract({
    address: asset.address,
    abi: ERC20_TRANSFER_ABI,
    functionName: "transfer",
    args: [p.to, p.amount],
  });

  return { hash };
}
