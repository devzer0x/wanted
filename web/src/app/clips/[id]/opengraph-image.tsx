import { ImageResponse } from "next/og";
import { fetchClip } from "@/lib/data";
import { formatDuration } from "@/lib/format";

export const alt = "WANTED — an auto-captured highlight clip";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

// Reads the clip row at request time; the CDN absorbs crawler traffic.
export const dynamic = "force-dynamic";

const CARD_CACHE_CONTROL = "public, max-age=0, s-maxage=300, stale-while-revalidate=86400";

// Satori supports flexbox only — no grid, no line-clamp — so long captions are trimmed here
// rather than allowed to overflow the card.
const CAPTION_MAX = 120;

// The arcade palette, matching src/app/globals.css. Satori cannot read CSS custom properties.
const INK = "#1B1B2F";
const CREAM = "#FFF6E5";
const CORAL = "#FF5E5B";
const MUTED = "#6B6B85";

function clamp(text: string): string {
  if (text.length <= CAPTION_MAX) return text;
  return `${text.slice(0, CAPTION_MAX - 1).trimEnd()}…`;
}

export default async function OgImage({ params }: { params: Promise<{ id: string }> }) {
  const { id: raw } = await params;
  const id = Number(raw);

  let caption = "Signal lost. Clip unavailable.";
  let meta = "";
  if (Number.isInteger(id) && id > 0) {
    const clip = await fetchClip(id);
    const row = clip.ok ? clip.rows[0] : undefined;
    if (row) {
      caption = clamp(row.caption || `Clip #${row.id}`);
      meta = `CLIP #${row.id} · ${formatDuration(row.duration_s)}`;
    } else {
      meta = `CLIP #${raw}`;
    }
  }

  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          backgroundColor: CREAM,
          color: INK,
          fontFamily: "sans-serif",
        }}
      >
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            flexGrow: 1,
            justifyContent: "space-between",
            padding: "54px 64px 40px",
          }}
        >
          <div style={{ display: "flex", alignItems: "flex-start" }}>
            <div
              style={{
                display: "flex",
                backgroundColor: CORAL,
                color: "#ffffff",
                border: `5px solid ${INK}`,
                borderRadius: 18,
                padding: "10px 30px",
                fontSize: 76,
                fontWeight: 900,
                letterSpacing: 2,
                lineHeight: 1.05,
                boxShadow: `0 9px 0 ${INK}`,
              }}
            >
              WANTED
            </div>
          </div>

          <div
            style={{
              display: "flex",
              flexDirection: "column",
              backgroundColor: "#ffffff",
              border: `5px solid ${INK}`,
              borderRadius: 22,
              boxShadow: `0 10px 0 ${INK}`,
              padding: "34px 38px",
            }}
          >
            <div style={{ display: "flex", fontSize: 42, fontWeight: 800, lineHeight: 1.25, maxWidth: 1000 }}>
              {caption}
            </div>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginTop: 26,
                fontSize: 21,
                letterSpacing: 3,
                color: MUTED,
              }}
            >
              <div style={{ display: "flex" }}>{meta}</div>
              <div style={{ display: "flex", color: CORAL }}>COMMENTARY IS AI-GENERATED</div>
            </div>
          </div>
        </div>

        <div
          style={{
            display: "flex",
            justifyContent: "center",
            padding: "16px 64px",
            backgroundColor: INK,
            color: CREAM,
            fontSize: 19,
            letterSpacing: 3,
          }}
        >
          AN AI PLAYS GTA · YOU CALL THE NEXT MOVE
        </div>
      </div>
    ),
    { ...size, headers: { "cache-control": CARD_CACHE_CONTROL } }
  );
}
