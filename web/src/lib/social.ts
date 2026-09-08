/** Wallet-address display helper, shared by the nav wallet button and the leaderboard.
 *
 * `$WANTED` itself has no deployed contract yet (CONTRACTS-PREDICTIONS §1 pins only the TTWO
 * reward asset). Rather than invent or zero-fill an address for the product token, nothing in
 * this file names one — surfaces that would depend on it stay hidden until there is a real
 * address to show, per the product brief.
 */

/** `0x7Sj8…4pump` — enough of both ends that someone comparing against another source can tell
 *  at a glance whether it is the same address. */
export function shortAddress(address: string, lead = 6, tail = 4): string {
  if (address.length <= lead + tail + 1) return address;
  return `${address.slice(0, lead)}…${address.slice(-tail)}`;
}
