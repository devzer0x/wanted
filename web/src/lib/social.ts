/** The project's public handles and addresses, in one place.
 *
 * The navbar, the footer and page metadata all want these, and a token address
 * typed twice is a token address that will eventually differ in one of the two
 * places — the kind of mistake that costs a viewer real money rather than a
 * broken link. One export, imported everywhere.
 */

/** The agent's account. Linked, never embedded: no third-party script on the site. */
export const X_URL = "https://x.com/wantedagent";
export const X_HANDLE = "@wantedagent";

/** The token's contract address. Displayed in full to anyone who wants to read
 * it, and copied in full regardless of how much of it the layout can show. */
export const CONTRACT_ADDRESS = "7Sj87Hw7Xb3hPfDWyRfThL2iEE8Bu7pcpJbbSYh4pump";

/** `7Sj87Hw7…SYh4pump` — enough of both ends that someone comparing against
 * another source can tell at a glance whether it is the same address. */
export function shortAddress(address: string, lead = 8, tail = 8): string {
  if (address.length <= lead + tail + 1) return address;
  return `${address.slice(0, lead)}…${address.slice(-tail)}`;
}
