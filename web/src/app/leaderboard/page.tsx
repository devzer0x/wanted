import type { Metadata } from "next";
import { LeaderboardView } from "@/components/leaderboard/LeaderboardView";
import { routeMetadata } from "@/lib/metadata";

export const metadata: Metadata = routeMetadata({
  path: "/leaderboard",
  title: "Leaderboard",
  description: "Who calls it best. Ranked by accuracy, streak, and TTWO earned.",
});

export default function LeaderboardPage() {
  return (
    <div className="flex flex-col gap-4 pt-4">
      <header className="flex flex-col gap-2">
        <h1 className="wordmark text-4xl sm:text-5xl" data-text="LEADERBOARD">
          LEADERBOARD
        </h1>
        <p className="max-w-2xl text-sm text-smoke">
          Every rank here is built from settled predictions — nothing is projected or padded.
        </p>
      </header>
      <LeaderboardView />
    </div>
  );
}
