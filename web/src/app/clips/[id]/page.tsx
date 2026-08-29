import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { CopyLinkButton } from "@/components/CopyLinkButton";
import { fetchClip } from "@/lib/data";
import { formatDuration, formatUtcStamp } from "@/lib/format";
import { routeMetadata } from "@/lib/metadata";
import { publicObjectUrl } from "@/lib/storage";

export const dynamic = "force-dynamic";

interface Props {
  params: Promise<{ id: string }>;
}

function parseId(raw: string): number | null {
  const id = Number(raw);
  return Number.isInteger(id) && id > 0 ? id : null;
}

const CLIP_DESCRIPTION = "An auto-captured WASTED highlight. The agent is an AI; the chaos is real.";

// A clip permalink is an explicit share surface with its own OG image, so it must carry its own
// og:title, og:url and canonical rather than inheriting the homepage's from the root layout.
// The caption may be unavailable (row missing, or the database unreachable); the identity of the
// route never is, so title/url/canonical stay clip-specific either way.
export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id: raw } = await params;
  const id = parseId(raw);
  // An unparseable id renders the 404 below, which resolves with the root layout's metadata; a
  // canonical here would just claim a URL that returns 404, so none is emitted.
  if (id === null) return { title: "Clip" };
  const clip = await fetchClip(id);
  const caption = clip.ok && clip.rows[0]?.caption ? clip.rows[0].caption : `Clip #${id}`;
  return routeMetadata({
    path: `/clips/${id}`,
    title: caption,
    description: CLIP_DESCRIPTION,
    // ./opengraph-image.tsx renders this clip's own card; naming images here would suppress it.
    hasOwnOgImage: true,
  });
}

export default async function ClipPage({ params }: Props) {
  const { id: raw } = await params;
  const id = parseId(raw);
  if (id === null) notFound();

  const clip = await fetchClip(id);

  if (!clip.ok) {
    return (
      <div className="flex flex-col gap-4 pt-4">
        <div className="panel px-4 py-12 text-center">
          <p className="wordmark text-2xl text-ember" data-text="DATA LINK DOWN">
            DATA LINK DOWN
          </p>
          <p className="mt-2 text-xs text-smoke">
            This clip can&apos;t be loaded right now — the telemetry database is unreachable.
          </p>
          <Link href="/clips" className="ticker mt-4 inline-block text-[0.65rem] text-smoke hover:text-ember">
            ← all clips
          </Link>
        </div>
      </div>
    );
  }

  const row = clip.rows[0];
  if (!row) notFound();

  const src = publicObjectUrl("clips", row.storage_path);

  return (
    <div className="flex flex-col gap-4 pt-4">
      <Link href="/clips" className="ticker text-[0.65rem] text-smoke hover:text-ember">
        ← all clips
      </Link>
      <article className="panel flex flex-col">
        {src ? (
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
        <div className="flex flex-col gap-2 p-4">
          <h1 className="font-display text-xl leading-tight text-bone [overflow-wrap:anywhere]">
            {row.caption || `Clip #${row.id}`}
          </h1>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-mono text-[0.65rem] text-smoke">
              {formatDuration(row.duration_s)} · {formatUtcStamp(row.ts)}
            </span>
            <CopyLinkButton path={`/clips/${row.id}`} />
          </div>
          <p className="text-[0.62rem] text-smoke">
            Captured automatically by the rig. The agent is an AI; commentary is AI-generated.
          </p>
        </div>
      </article>
    </div>
  );
}
