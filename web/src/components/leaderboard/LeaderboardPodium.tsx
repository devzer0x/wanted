import { formatBaseUnits, formatInt } from "@/lib/format";
import { shortAddress } from "@/lib/social";
import type { LeaderboardRow } from "@/lib/prediction/types";
import { accuracyLabel, walletInitials } from "@/components/leaderboard/display";

// Colour, desktop column and podium lift by finishing position. The DOM stays in rank order — a
// screen reader reads 1, 2, 3 — and CSS `order` alone puts #1 in the middle at >= 640px, standing
// a step above the other two.
const PLACES = [
  { fill: "var(--yellow)", column: "sm:order-2", lift: "sm:translate-y-0" },
  { fill: "var(--blue)", column: "sm:order-1", lift: "sm:translate-y-3" },
  { fill: "var(--teal)", column: "sm:order-3", lift: "sm:translate-y-5" },
] as const;

// Static class strings: Tailwind cannot see a template-built one. The podium never holds more than
// three cards, and with one or two real rows it simply narrows rather than padding itself out.
const COLUMNS: Record<number, string> = {
  1: "sm:grid-cols-1 sm:max-w-sm",
  2: "sm:grid-cols-2 sm:max-w-2xl",
  3: "sm:grid-cols-3",
};

function PodiumStat({ value, label, accent }: { value: string; label: string; accent?: boolean }) {
  return (
    <span className="flex-1 rounded-[10px] border-2 border-ink bg-white px-2 py-1.5 text-center">
      <span
        className={`block font-display text-lg leading-none ${accent ? "text-coral" : "text-ink"}`}
      >
        {value}
      </span>
      <span className="mt-0.5 block text-[9px] font-extrabold uppercase tracking-[0.1em] text-muted">
        {label}
      </span>
    </span>
  );
}

/** The top three finishers. `rows` is already the real top slice — never padded to three. */
export function LeaderboardPodium({ rows }: { rows: LeaderboardRow[] }) {
  if (rows.length === 0) return null;

  return (
    <ol
      className={`grid grid-cols-1 items-start gap-3.5 ${COLUMNS[rows.length] ?? "sm:grid-cols-3"}`}
      aria-label="Top of the leaderboard"
    >
      {rows.map((row, index) => {
        const place = PLACES[index] ?? PLACES[PLACES.length - 1];
        const first = index === 0;
        return (
          <li
            key={row.wallet}
            data-testid="leaderboard-podium-card"
            className={`flex flex-col gap-2.5 rounded-[22px] border-[3px] border-ink shadow-[0_7px_0_var(--ink)] ${
              first ? "p-4 sm:p-5" : "p-4"
            } ${place.column} ${place.lift}`}
            style={{ background: place.fill }}
          >
            <div className="flex items-center justify-between gap-2">
              <span
                className={`font-display leading-none ${first ? "text-[42px]" : "text-[36px]"}`}
              >
                #{formatInt(row.rank)}
              </span>
              <span className="pill pill-sm">{accuracyLabel(row.accuracy)} acc</span>
            </div>

            <div className="flex min-w-0 items-center gap-2.5">
              <span
                aria-hidden="true"
                className="flex h-11 w-11 flex-none items-center justify-center rounded-[14px] border-[3px] border-ink bg-white font-display text-base"
              >
                {walletInitials(row.wallet)}
              </span>
              <span className="min-w-0 truncate text-[15px] font-black" title={row.wallet}>
                {shortAddress(row.wallet, 6, 4)}
              </span>
            </div>

            <div className="flex gap-2">
              <PodiumStat value={formatInt(row.current_streak)} label="Streak" accent />
              <PodiumStat value={formatBaseUnits(row.earned)} label="TTWO" />
            </div>
          </li>
        );
      })}
    </ol>
  );
}
