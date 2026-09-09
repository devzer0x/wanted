"use client";

import { formatBaseUnits, formatInt } from "@/lib/format";
import { shortAddress } from "@/lib/social";
import type { LeaderboardRow, LeaderboardWindow } from "@/lib/prediction/types";
import { LeaderboardEmpty } from "@/components/leaderboard/LeaderboardEmpty";
import { accuracyLabel, walletChipColor, walletInitials } from "@/components/leaderboard/display";

// Mobile is the tight case: at 390px the five columns have to leave the shortened address room
// for BOTH ends of the wallet, which is the only reason to show a shortened address at all.
const HEAD_CELL =
  "px-1.5 py-2.5 text-[10px] font-extrabold uppercase tracking-[0.12em] text-muted sm:px-4";
const CELL = "px-1.5 py-2.5 align-middle sm:px-4";

export function LeaderboardTable({
  rows,
  window,
}: {
  rows: LeaderboardRow[];
  window: LeaderboardWindow;
}) {
  if (rows.length === 0) return <LeaderboardEmpty window={window} />;

  return (
    <div className="panel overflow-hidden rounded-[22px] shadow-[0_7px_0_var(--ink)]">
      <table className="w-full table-fixed border-collapse text-left">
        <caption className="sr-only">
          Wallets ranked by the accuracy of their settled predictions.
        </caption>
        <thead>
          <tr className="border-b-[3px] border-ink bg-sand">
            <th scope="col" className={`${HEAD_CELL} w-7 sm:w-12`}>
              #
            </th>
            <th scope="col" className={HEAD_CELL}>
              Player
            </th>
            <th scope="col" className={`${HEAD_CELL} w-[52px] text-right sm:w-20`}>
              Acc
            </th>
            <th scope="col" className={`${HEAD_CELL} w-14 text-right sm:w-20`}>
              Streak
            </th>
            <th scope="col" className={`${HEAD_CELL} w-[66px] text-right sm:w-20`}>
              TTWO
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.wallet}
              data-testid="leaderboard-row"
              // The leader's row is tinted cream so the table's top and the podium's centre read
              // as the same wallet.
              className={`border-b-2 border-sand last:border-b-0 ${
                row.rank === 1 ? "bg-cream" : "bg-white"
              }`}
            >
              <td className={`${CELL} font-display text-lg leading-none`}>{formatInt(row.rank)}</td>
              <td className={CELL}>
                <span className="flex min-w-0 items-center gap-2 sm:gap-2.5">
                  <span
                    aria-hidden="true"
                    className="flex h-7 w-7 flex-none items-center justify-center rounded-[10px] border-2 border-ink font-display text-[11px] leading-none sm:h-[30px] sm:w-[30px] sm:text-xs"
                    style={{ background: walletChipColor(row.wallet) }}
                  >
                    {walletInitials(row.wallet)}
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate text-xs font-black sm:text-sm" title={row.wallet}>
                      {shortAddress(row.wallet, 6, 4)}
                    </span>
                    <span className="block text-[11px] font-bold text-muted">
                      {formatInt(row.correct)} / {formatInt(row.total)} correct
                    </span>
                  </span>
                </span>
              </td>
              <td className={`${CELL} whitespace-nowrap text-right text-[13px] font-black sm:text-sm`}>
                {accuracyLabel(row.accuracy)}
              </td>
              {/* The design's streak column carries one number. The row also has a real best
                  streak, so it rides along as the sub-label — the same two-line idiom the player
                  cell uses — rather than being dropped or given a column of its own. */}
              <td className={`${CELL} text-right`}>
                <span className="block text-[13px] font-black text-coral sm:text-sm">
                  {formatInt(row.current_streak)}
                </span>
                <span className="block whitespace-nowrap text-[10px] font-bold text-muted">
                  best {formatInt(row.best_streak)}
                </span>
              </td>
              <td className={`${CELL} whitespace-nowrap text-right text-[13px] font-black sm:text-sm`}>
                {formatBaseUnits(row.earned)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
