import Link from "next/link";

// Without this file Next.js serves its own default 404, which lands inside our layout and looks
// like a broken page. Same sticker vocabulary as every other page title.
export default function NotFound() {
  return (
    <div className="flex flex-col items-start gap-6 pt-10 pb-16">
      <h1 className="m-0">
        <span className="page-title bg-coral text-white">WRONG TURN</span>
      </h1>
      <p className="max-w-xl text-[15px] leading-[1.5] font-bold text-dim">
        There is nothing at this address. No clip, no mission, no page — and we would rather say so
        than show you something we made up.
      </p>
      <div className="flex flex-wrap gap-3">
        <Link href="/" className="btn btn-coral">
          Back to the live page
        </Link>
        <Link href="/clips" className="btn">
          Clips
        </Link>
      </div>
    </div>
  );
}
