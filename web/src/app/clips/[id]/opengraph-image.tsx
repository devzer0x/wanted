import { ImageResponse } from "next/og";
import { fetchClip } from "@/lib/data";
import { formatDuration } from "@/lib/format";

export const alt = "WANTED — an auto-captured highlight clip";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

// Satori supports flexbox only — no grid, no CSS shorthand tricks beyond its subset.
export default async function OgImage({ params }: { params: Promise<{ id: string }> }) {
  const { id: raw } = await params;
  const id = Number(raw);

  let caption = "Signal lost. Clip unavailable.";
  let meta = "";
  if (Number.isInteger(id) && id > 0) {
    const clip = await fetchClip(id);
    const row = clip.ok ? clip.rows[0] : undefined;
    if (row) {
      caption = row.caption || `Clip #${row.id}`;
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
          backgroundColor: "#070606",
          color: "#ece4d4",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", height: 18, width: "100%", backgroundColor: "#e02418" }} />
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            flexGrow: 1,
            justifyContent: "space-between",
            padding: "56px 64px",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column" }}>
            <div
              style={{
                display: "flex",
                fontSize: 120,
                fontWeight: 800,
                letterSpacing: 2,
                transform: "skewX(-6deg)",
                textShadow: "8px 6px 0 #e02418",
              }}
            >
              WASTED
            </div>
            <div
              style={{
                display: "flex",
                marginTop: 12,
                fontSize: 26,
                letterSpacing: 8,
                color: "#93897a",
              }}
            >
              AN AI PLAYS. FOREVER.
            </div>
          </div>
          <div style={{ display: "flex", flexDirection: "column" }}>
            <div
              style={{
                display: "flex",
                fontSize: 44,
                lineHeight: 1.25,
                maxWidth: 1000,
                color: "#ece4d4",
              }}
            >
              {caption}
            </div>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                marginTop: 28,
                fontSize: 22,
                letterSpacing: 4,
                color: "#93897a",
              }}
            >
              <div style={{ display: "flex" }}>{meta}</div>
              <div style={{ display: "flex", color: "#e02418" }}>
                WANTED IS AN AI · COMMENTARY IS AI-GENERATED
              </div>
            </div>
          </div>
        </div>
        <div style={{ display: "flex", height: 18, width: "100%", backgroundColor: "#e02418" }} />
      </div>
    ),
    size
  );
}
