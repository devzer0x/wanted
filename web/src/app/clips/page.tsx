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

function ClipCard({ clip }: { clip: ClipRow }) {
  const src = publicObjectUrl("clips", clip.storage_path);
  return (
    <article className="panel group flex flex-col transition-colors hover:border-blood">
      <div className="relative">
        {src ? (
          // preload="metadata" keeps a gallery of clips cheap on a bad connection: the browser
          // fetches the header for a poster frame and duration, not the video body.
          <video
            src={src}
            controls
            muted
            playsInline
            preload="metadata"
            className="aspect-video w-full bg-void object-contain"
          />
        ) : (
          <div className="flex aspect-video w-full items-center justify-center bg-void">
            <span className="ticker text-[0.6rem] text-smoke">clip source unavailable</span>
          </div>
        )}
        <span className="ticker pointer-events-none absolute right-1.5 top-1.5 border border-ash bg-void/85 px-1.5 py-0.5 font-mono text-[0.55rem] text-bone">
          {formatDuration(clip.duration_s)}
        </span>
      </div>
      <div className="flex flex-1 flex-col gap-2 p-3">
        <p className="flex-1 text-sm leading-snug text-bone [overflow-wrap:anywhere]">
          {clip.caption || "Untitled chaos"}
        </p>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="font-mono text-[0.6rem] text-smoke">{formatUtcStamp(clip.ts)}</span>
          <div className="flex items-center gap-2">
            <CopyLinkButton path={`/clips/${clip.id}`} />
            <Link
              href={`/clips/${clip.id}`}
              className="ticker border border-ash px-2 py-1 text-[0.6rem] text-smoke transition-colors hover:border-blood hover:text-ember"
            >
              open
            </Link>
          </div>
        </div>
      </div>
    </article>
  );
}

export default async function ClipsPage() {
  const clips = await fetchClips();
  const rows = clips.ok ? clips.rows : [];

  return (
    <div className="flex flex-col gap-4 pt-4">
      <header className="flex flex-col gap-2">
        <h1 className="wordmark text-4xl sm:text-5xl" data-text="CLIPS">
          CLIPS
        </h1>
        <p className="max-w-2xl text-sm text-smoke">
          Thirty-second replays saved automatically when something spectacular (or spectacularly
          stupid) happens. Newest first.
        </p>
      </header>

      {rows.length === 0 ? (
        <div className="panel px-4 py-12 text-center" data-testid="clips-empty">
          {clips.ok ? (
            <>
              <p className="wordmark text-2xl" data-text="NO FOOTAGE">
                NO FOOTAGE
              </p>
              <p className="mt-2 text-xs text-smoke">
                Nothing clip-worthy has happened yet. Give him time.
              </p>
            </>
          ) : (
            <>
              <p className="wordmark text-2xl text-ember" data-text="DATA LINK DOWN">
                DATA LINK DOWN
              </p>
              <p className="mt-2 text-xs text-smoke">
                Clip library unavailable — the telemetry database is unreachable.
              </p>
            </>
          )}
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {rows.map((clip) => (
            <ClipCard key={clip.id} clip={clip} />
          ))}
        </div>
      )}
    </div>
  );
}
