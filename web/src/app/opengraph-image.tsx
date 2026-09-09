import { ImageResponse } from "next/og";
import { fetchStats } from "@/lib/data";
import { formatInt } from "@/lib/format";
import { SITE_OG_ALT } from "@/lib/metadata";
import { isAgentOffline } from "@/lib/offline";

// Shared with src/lib/metadata.ts, which names this card explicitly on every route that does not
// ship its own (Next drops an inherited og:image the moment a route declares `openGraph`).
export const alt = SITE_OG_ALT;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

// The card reads the live stats row, so it can never be prerendered; saying so keeps the build
// log clean. Crawler traffic is absorbed by the CDN cache header below rather than by rendering
// a fresh card per request.
export const dynamic = "force-dynamic";

const CARD_CACHE_CONTROL = "public, max-age=0, s-maxage=300, stale-while-revalidate=86400";

// The arcade palette, matching src/app/globals.css. Satori cannot read CSS custom properties, so
// the values are repeated here rather than referenced.
const INK = "#1B1B2F";
const CREAM = "#FFF6E5";
const SAND = "#F1E9D8";
const CORAL = "#FF5E5B";
const YELLOW = "#FFC93C";
const TEAL = "#2ED3B7";
const MUTED = "#6B6B85";

// Satori supports a flexbox subset only: every container needs an explicit display, and there is
// no grid. Counters come from the real stats row; when it is unavailable the card falls back to
// the wordmark and tagline rather than inventing numbers.
function Counter({ value, label, tint }: { value: string; label: string; tint: string }) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        marginRight: 20,
        padding: "16px 26px",
        backgroundColor: "#ffffff",
        border: `4px solid ${INK}`,
        borderRadius: 18,
        boxShadow: `0 7px 0 ${INK}`,
      }}
    >
      <div style={{ display: "flex", fontSize: 64, fontWeight: 900, color: tint, lineHeight: 1 }}>
        {value}
      </div>
      <div style={{ display: "flex", marginTop: 6, fontSize: 18, letterSpacing: 3, color: MUTED }}>
        {label}
      </div>
    </div>
  );
}

export default async function OgImage() {
  const stats = await fetchStats();
  const row = stats.ok ? (stats.rows[0] ?? null) : null;
  const offline = isAgentOffline(row, Date.now());

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
            padding: "56px 64px 44px",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
            <div
              style={{
                display: "flex",
                backgroundColor: CORAL,
                color: "#ffffff",
                border: `6px solid ${INK}`,
                borderRadius: 22,
                padding: "14px 40px",
                fontSize: 118,
                fontWeight: 900,
                letterSpacing: 2,
                lineHeight: 1.05,
                boxShadow: `0 12px 0 ${INK}`,
              }}
            >
              WANTED
            </div>
            <div
              style={{
                display: "flex",
                marginTop: 36,
                fontSize: 34,
                fontWeight: 800,
                color: INK,
              }}
            >
              An AI plays GTA. You call the next move.
            </div>
            <div style={{ display: "flex", marginTop: 10, fontSize: 25, color: MUTED }}>
              Free to play · correct picks share the TTWO pot
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between" }}>
            {row ? (
              <div style={{ display: "flex" }}>
                <Counter value={formatInt(row.deaths)} label="DEATHS" tint={CORAL} />
                <Counter value={formatInt(row.busted)} label="BUSTED" tint={CORAL} />
                <Counter value={formatInt(row.missions_passed)} label="MISSIONS" tint={INK} />
              </div>
            ) : (
              <div style={{ display: "flex", fontSize: 26, color: MUTED, maxWidth: 620 }}>
                It drives, it crashes, it explains itself.
              </div>
            )}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                backgroundColor: offline ? SAND : YELLOW,
                border: `4px solid ${INK}`,
                borderRadius: 999,
                padding: "12px 26px",
                fontSize: 24,
                fontWeight: 900,
                letterSpacing: 3,
                color: INK,
                boxShadow: `0 6px 0 ${INK}`,
              }}
            >
              {offline ? "OFF AIR" : "● LIVE"}
            </div>
          </div>
        </div>

        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "18px 64px",
            backgroundColor: INK,
            color: CREAM,
            fontSize: 19,
            letterSpacing: 3,
          }}
        >
          <div style={{ display: "flex" }}>COMMENTARY IS AI-GENERATED</div>
          <div style={{ display: "flex", color: TEAL }}>$WANTED / TTWO · ROBINHOOD CHAIN</div>
        </div>
      </div>
    ),
    { ...size, headers: { "cache-control": CARD_CACHE_CONTROL } }
  );
}
