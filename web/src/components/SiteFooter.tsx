import Link from "next/link";
import { Wordmark } from "@/components/Wordmark";

export function SiteFooter() {
  return (
    <footer className="border-t border-ash bg-tar">
      <div className="stripes-dim h-1" aria-hidden="true" />
      <div className="mx-auto w-full max-w-6xl px-3 sm:px-5 py-8 flex flex-col gap-4">
        <Wordmark className="text-xl" />
        <p className="text-xs leading-relaxed text-smoke max-w-2xl">
          WANTED is an independent art-and-engineering experiment. It is not affiliated with,
          endorsed by, or connected to Rockstar Games or Take-Two Interactive. The agent is an
          AI; all commentary and gameplay decisions on this site are AI-generated.
        </p>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.68rem] ticker text-smoke">
          <Link href="/predict" className="hover:text-ember">
            Predict
          </Link>
          <Link href="/leaderboard" className="hover:text-ember">
            Leaderboard
          </Link>
          <Link href="/agent" className="hover:text-ember">
            Who&apos;s playing?
          </Link>
          <Link href="/missions" className="hover:text-ember">
            Story progress
          </Link>
          <Link href="/clips" className="hover:text-ember">
            Clips
          </Link>
          <span className="text-ash" aria-hidden="true">
            {"//"}
          </span>
          <span>Single-player story mode only · cheats are announced, never hidden</span>
        </div>
      </div>
    </footer>
  );
}
