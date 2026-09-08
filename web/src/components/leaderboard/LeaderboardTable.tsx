"use client";

import { formatBaseUnits, formatInt } from "@/lib/format";
import { shortAddress } from "@/lib/social";
import type { LeaderboardRow } from "@/lib/prediction/types";

// `accuracy` is a fraction in [0, 1] — `public.leaderboard()` returns `correct / total` rounded to
// 4 places (verified against the real function: 0.6667 for 2 correct of 3), and that is now stated
// on `LeaderboardRow`. The `<= 1` branch is kept anyway: it costs nothing, and it is correct at
// both ends (a perfect 1.0 renders 100%, and a hypothetical already-percentage value passes
// through) — so a future change of convention shows up as a wrong-looking number here rather than
// as a silent factor-of-100 error in front of viewers comparing their rank.
function accuracyLabel(accuracy: number): string {
  if (!Number.isFinite(accuracy)) return "—";
  const pct = accuracy <= 1 ? accuracy * 100 : accuracy;
  // One decimal below 10% so 3.2% and 3.8% are distinguishable, but not for an exact 0 or 100 —
  // "0.0%" beside "100%" in the same column reads as a formatting bug rather than as precision.
  if (pct === 0 || pct === 100) return `${pct}%`;
  return `${pct.toFixed(pct < 10 ? 1 : 0)}%`;
}

export function LeaderboardTable({ rows }: { rows: LeaderboardRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="panel px-4 py-12 text-center" data-testid="leaderboard-empty">
        <p className="wordmark text-2xl" data-text="NO RANKINGS YET">
          NO RANKINGS YET
        </p>
        <p className="mt-2 text-xs text-smoke">
          Nobody has a settled prediction in this window yet.
        </p>
      </div>
    );
  }

  return (
    <div className="panel overflow-x-auto">
      <table className="w-full min-w-[680px] border-collapse text-left">
        <thead>
          <tr className="border-b border-ash">
            <th className="ticker px-3 py-2 text-[0.58rem] text-smoke">Rank</th>
            <th className="ticker px-3 py-2 text-[0.58rem] text-smoke">Wallet</th>
            <th className="ticker px-3 py-2 text-right text-[0.58rem] text-smoke">Accuracy</th>
            <th className="ticker px-3 py-2 text-right text-[0.58rem] text-smoke">Correct</th>
            <th className="ticker px-3 py-2 text-right text-[0.58rem] text-smoke">Streak</th>
            <th className="ticker px-3 py-2 text-right text-[0.58rem] text-smoke">Best streak</th>
            <th className="ticker px-3 py-2 text-right text-[0.58rem] text-smoke">TTWO earned</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.wallet} className="border-b border-ash/60 last:border-0" data-testid="leaderboard-row">
              <td className="px-3 py-2 font-mono text-sm text-bone">{row.rank}</td>
              <td className="px-3 py-2 font-mono text-xs text-bone" title={row.wallet}>
                {shortAddress(row.wallet, 6, 4)}
              </td>
              <td className="px-3 py-2 text-right font-mono text-xs text-bone">
                {accuracyLabel(row.accuracy)}
              </td>
              <td className="px-3 py-2 text-right font-mono text-xs text-smoke">
                {formatInt(row.correct)} / {formatInt(row.total)}
              </td>
              <td className="px-3 py-2 text-right font-mono text-xs text-ember">
                {formatInt(row.current_streak)}
              </td>
              <td className="px-3 py-2 text-right font-mono text-xs text-smoke">
                {formatInt(row.best_streak)}
              </td>
              <td className="px-3 py-2 text-right font-mono text-xs text-bone">
                {formatBaseUnits(row.earned)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
