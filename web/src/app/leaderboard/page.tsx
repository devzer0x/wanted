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
    <div className="flex flex-col gap-4 pt-6">
      <LeaderboardView
        heading={
          <div>
            <h1 className="m-0 leading-none">
              <span className="page-title bg-blue">LEADERBOARD</span>
            </h1>
            <p className="mt-4 max-w-[560px] text-[15px] leading-snug text-dim">
              Built only from settled predictions. Nothing projected, nothing padded.
            </p>
          </div>
        }
      />
    </div>
  );
}
