import { defineChain } from "viem";

// UI-layer chain metadata for the wallet-connect flow only: display strings and the params
// needed for `wallet_switchEthereumChain` / `wallet_addEthereumChain`. This is NOT the asset
// configuration layer (CONTRACTS-PREDICTIONS §1: that lives in `web/src/lib/chain/assets.ts`
// and the `asset` DB columns, which this file never touches) — it only gets a wallet onto the
// right network so a signature can be requested on it.
//
// Verified 2026-09-08 by direct `eth_chainId` against the mainnet RPC (returned `0x1237`) and
// cross-checked against docs.robinhood.com/chain (CONTRACTS-PREDICTIONS §1, §8).
export const WANTED_CHAIN_ID = 4663;
export const WANTED_CHAIN_ID_HEX = "0x1237";

export const wantedChain = defineChain({
  id: WANTED_CHAIN_ID,
  name: "Robinhood Chain",
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: { default: { http: ["https://rpc.mainnet.chain.robinhood.com"] } },
  blockExplorers: {
    default: { name: "Blockscout", url: "https://robinhoodchain.blockscout.com" },
  },
});
