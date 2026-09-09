import type { Metadata } from "next";
import Link from "next/link";
import { CopyLinkButton } from "@/components/CopyLinkButton";
import { fetchClips } from "@/lib/data";
import { formatDuration, formatUtcStamp } from "@/lib/format";
import { routeMetadata } from "@/lib/metadata";
import { publicObjectUrl } from "@/lib/storage";
import type { ClipRow } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = routeMetadata({
  path: "/clips",
  title: "Clips",
  description: "Auto-captured highlights of the agent's finest disasters, newest first.",
});

// CopyLinkButton is shared (src/components/CopyLinkButton.tsx) and is not this route's to edit, so
// the sticker look is applied to it from here. `!` is needed because the button carries its own
// size/border utilities and Tailwind's utility layer has no source-order guarantee between two
// candidates that set the same property.
const SHARE_BUTTON_SKIN =
  "contents [&>button]:inline-flex [&>button]:h-8 [&>button]:items-center [&>button]:justify-center [&>button]:rounded-[10px]! [&>button]:border-[3px]! [&>button]:border-ink! [&>button]:bg-white! [&>button]:px-3! [&>button]:text-[12px]! [&>button]:text-ink [&>button]:shadow-[0_3px_0_var(--ink)]!";

// `hover:text-ink!` is important on purpose: globals.css's unlayered `a:hover { color: coral }`
// beats any layered utility, and a coral label on the blue fill is not the design.
const WATCH_BUTTON =
  "inline-flex h-8 items-center justify-center rounded-[10px] border-[3px] border-ink bg-blue px-3 font-display text-[12px] uppercase tracking-[0.04em] text-ink shadow-[0_3px_0_var(--ink)] transition-transform hover:-translate-y-px hover:text-ink! active:translate-y-[2px] active:shadow-[0_1px_0_var(--ink)]";

function ClipCard({ clip }: { clip: ClipRow }) {
  const src = publicObjectUrl("clips", clip.storage_path);
  return (
    <article className="panel flex flex-col overflow-hidden transition-transform hover:-translate-y-0.5">
      <div className="relative border-b-[3px] border-ink">
        {src ? (
          // preload="metadata" keeps a gallery of clips cheap on a bad connection: the browser
          // fetches the header for a poster frame and duration, not the video body.
          <video
            src={src}
            controls
            muted
            playsInline
            preload="metadata"
            className="aspect-video w-full bg-ink-deep object-contain"
          />
        ) : (
          <div className="flex aspect-video w-full items-center justify-center bg-ink-deep px-3">
            <span className="pill pill-sm panel-title text-center">clip source unavailable</span>
          </div>
        )}
        <span className="pill pill-sm panel-title pointer-events-none absolute right-2.5 top-2.5">
          {formatDuration(clip.duration_s)}
        </span>
      </div>
      <div className="flex flex-1 flex-col gap-2.5 p-3.5">
        <p className="flex-1 text-[15px] font-black leading-snug [overflow-wrap:anywhere]">
          {clip.caption || "Untitled chaos"}
        </p>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[11px] font-extrabold text-muted">{formatUtcStamp(clip.ts)}</span>
          <div className="flex items-center gap-1.5">
            <span className={SHARE_BUTTON_SKIN}>
              <CopyLinkButton path={`/clips/${clip.id}`} />
            </span>
            <Link href={`/clips/${clip.id}`} className={WATCH_BUTTON}>
              Watch
            </Link>
          </div>
        </div>
      </div>
    </article>
  );
}

function ClipsEmptyState({
  title,
  body,
  tone,
}: {
  title: string;
  body: string;
  tone: "sand" | "coral";
}) {
  return (
    <>
      <p
        className={`inline-block rotate-[-1.5deg] rounded-[14px] border-[3px] border-ink px-5 py-2 font-display text-2xl uppercase shadow-[0_5px_0_var(--ink)] ${
          tone === "coral" ? "bg-coral text-white" : "bg-sand"
        }`}
      >
        {title}
      </p>
      <p className="mx-auto mt-4 max-w-sm text-sm leading-relaxed text-dim">{body}</p>
    </>
  );
}

export default async function ClipsPage() {
  const clips = await fetchClips();
  const rows = clips.ok ? clips.rows : [];

  return (
    <div className="flex flex-col gap-5 pt-6">
      <header className="flex flex-col gap-3">
        <h1>
          <span className="page-title panel-coral text-white">Clips</span>
        </h1>
        <p className="max-w-xl text-[15px] leading-relaxed text-dim">
          Thirty-second replays saved automatically when something spectacular (or spectacularly
          stupid) happens. Newest first.
        </p>
      </header>

      {rows.length === 0 ? (
        <div className="panel px-5 py-14 text-center" data-testid="clips-empty">
          {clips.ok ? (
            <ClipsEmptyState
              title="No footage"
              body="Nothing clip-worthy has happened yet. Give him time."
              tone="sand"
            />
          ) : (
            <ClipsEmptyState
              title="Data link down"
              body="Clip library unavailable — the telemetry database is unreachable."
              tone="coral"
            />
          )}
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {rows.map((clip) => (
            <ClipCard key={clip.id} clip={clip} />
          ))}
        </div>
      )}
    </div>
  );
}
