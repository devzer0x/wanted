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

const CLIP_DESCRIPTION = "An auto-captured WANTED highlight. The agent is an AI; the chaos is real.";

// CopyLinkButton is shared (src/components/CopyLinkButton.tsx) and is not this route's to edit, so
// the sticker look is applied to it from here. `!` is needed because the button carries its own
// size/border utilities and Tailwind's utility layer has no source-order guarantee between two
// candidates that set the same property.
const SHARE_BUTTON_SKIN =
  "contents [&>button]:inline-flex [&>button]:h-9 [&>button]:items-center [&>button]:justify-center [&>button]:rounded-[10px]! [&>button]:border-[3px]! [&>button]:border-ink! [&>button]:bg-white! [&>button]:px-4! [&>button]:text-[13px]! [&>button]:text-ink [&>button]:shadow-[0_3px_0_var(--ink)]!";

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

function BackLink() {
  return (
    <Link
      href="/clips"
      className="pill panel-title w-fit shadow-[0_3px_0_var(--ink)] transition-transform hover:-translate-y-px"
    >
      ← All clips
    </Link>
  );
}

export default async function ClipPage({ params }: Props) {
  const { id: raw } = await params;
  const id = parseId(raw);
  if (id === null) notFound();

  const clip = await fetchClip(id);

  if (!clip.ok) {
    return (
      <div className="flex flex-col items-start gap-5 pt-6">
        <BackLink />
        <div className="panel w-full px-5 py-14 text-center">
          <p className="inline-block rotate-[-1.5deg] rounded-[14px] border-[3px] border-ink bg-coral px-5 py-2 font-display text-2xl uppercase text-white shadow-[0_5px_0_var(--ink)]">
            Data link down
          </p>
          <p className="mx-auto mt-4 max-w-sm text-sm leading-relaxed text-dim">
            This clip can&apos;t be loaded right now — the telemetry database is unreachable.
          </p>
        </div>
      </div>
    );
  }

  const row = clip.rows[0];
  if (!row) notFound();

  const src = publicObjectUrl("clips", row.storage_path);

  return (
    <div className="flex flex-col items-start gap-5 pt-6">
      <BackLink />
      <article className="panel flex w-full flex-col overflow-hidden">
        <div className="relative border-b-[3px] border-ink">
          {src ? (
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
            {formatDuration(row.duration_s)}
          </span>
        </div>
        <div className="flex flex-col gap-3 p-4 sm:p-5">
          <h1 className="font-display text-[26px] leading-tight [overflow-wrap:anywhere]">
            {row.caption || `Clip #${row.id}`}
          </h1>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span className="text-[12px] font-extrabold text-muted">
              {formatDuration(row.duration_s)} · {formatUtcStamp(row.ts)}
            </span>
            <span className={SHARE_BUTTON_SKIN}>
              <CopyLinkButton path={`/clips/${row.id}`} />
            </span>
          </div>
          <p className="border-t-2 border-dashed border-sand-deep pt-3 text-[12px] leading-relaxed text-muted">
            Captured automatically by the rig. The agent is an AI; commentary is AI-generated.
          </p>
        </div>
      </article>
    </div>
  );
}
