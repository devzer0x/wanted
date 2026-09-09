import Link from "next/link";
import type { LeaderboardWindow } from "@/lib/prediction/types";

// The board is a view over settled predictions, so "empty" has exactly one honest meaning per
// window: nothing has resolved in it yet. Each string says that and stops — no placeholder rows,
// no zeroed podium, no "coming soon".
const EMPTY_HEADLINE: Record<LeaderboardWindow, string> = {
  today: "Nothing settled today",
  week: "Nothing settled this week",
  all: "No settled calls yet",
};

const EMPTY_BODY: Record<LeaderboardWindow, string> = {
  today: "No prediction has resolved since midnight UTC, so there is nothing to rank yet.",
  week: "No prediction has resolved in the last seven days, so there is nothing to rank yet.",
  all: "No prediction has resolved yet. The first wallet to call one correctly starts the board.",
};

export function LeaderboardEmpty({ window }: { window: LeaderboardWindow }) {
  return (
    <div
      className="panel rounded-[22px] px-5 py-12 text-center shadow-[0_7px_0_var(--ink)] sm:px-8 sm:py-16"
      data-testid="leaderboard-empty"
    >
      <p className="m-0">
        <span className="panel-title inline-block -rotate-1 rounded-[14px] border-[3px] border-ink bg-yellow px-4 py-2 text-[clamp(20px,4vw,28px)] leading-none shadow-[0_5px_0_var(--ink)]">
          {EMPTY_HEADLINE[window]}
        </span>
      </p>
      <p className="mx-auto mt-5 max-w-md text-[15px] leading-relaxed text-dim">
        {EMPTY_BODY[window]}
      </p>
      <Link href="/predict" className="btn btn-coral mt-6">
        Go to predict
      </Link>
    </div>
  );
}
