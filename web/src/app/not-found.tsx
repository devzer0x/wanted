import Link from "next/link";

// Without this file Next.js serves its default light-themed 404, which lands inside our black
// layout and looks like a broken page.
export default function NotFound() {
  return (
    <div className="flex flex-col items-start gap-5 pt-10 pb-16">
      <div className="stripes h-1.5 w-full max-w-xl" aria-hidden="true" />
      <h1 className="wordmark text-5xl text-ember sm:text-6xl" data-text="WRONG TURN">
        WRONG TURN
      </h1>
      <p className="max-w-xl text-sm leading-relaxed text-smoke">
        There is nothing at this address. No clip, no mission, no page — and we would rather say so
        than show you something we made up.
      </p>
      <div className="flex flex-wrap gap-2">
        <Link
          href="/"
          className="ticker border border-blood px-3 py-2 text-[0.65rem] text-ember transition-colors hover:bg-blood hover:text-bone"
        >
          Back to the live page
        </Link>
        <Link
          href="/clips"
          className="ticker border border-ash px-3 py-2 text-[0.65rem] text-smoke transition-colors hover:border-blood hover:text-ember"
        >
          Clips
        </Link>
      </div>
    </div>
  );
}
