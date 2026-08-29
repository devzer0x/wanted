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

const VOID = "#070606";
const BONE = "#ece4d4";
const SMOKE = "#93897a";
const BLOOD = "#e02418";
const EMBER = "#ff4a35";
const ASH = "#262220";

// Satori supports a flexbox subset only: every container needs an explicit display, and there is
// no grid. Counters come from the real stats row; when it is unavailable the card falls back to
// the wordmark and tagline rather than inventing numbers.
function Counter({ value, label, loud }: { value: string; label: string; loud?: boolean }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", marginRight: 56 }}>
      <div style={{ display: "flex", fontSize: 72, fontWeight: 800, color: loud ? EMBER : BONE }}>
        {value}
      </div>
      <div style={{ display: "flex", fontSize: 20, letterSpacing: 6, color: SMOKE }}>{label}</div>
    </div>
  );
}

export default async function OgImage() {
  const stats = await fetchStats();
  const row = stats.ok ? (stats.rows[0] ?? null) : null;
  const offline = isAgentOffline(row, Date.now());
  const statusText = offline ? "OFF THE AIR" : "ON THE AIR";

  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          backgroundColor: VOID,
          color: BONE,
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", height: 18, width: "100%", backgroundColor: BLOOD }} />
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            flexGrow: 1,
            justifyContent: "space-between",
            padding: "52px 64px",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column" }}>
            <div
              style={{
                display: "flex",
                fontSize: 156,
                fontWeight: 800,
                letterSpacing: 2,
                lineHeight: 1,
                transform: "skewX(-6deg)",
                textShadow: `10px 8px 0 ${BLOOD}`,
              }}
            >
              WASTED
            </div>
            <div
              style={{
                display: "flex",
                marginTop: 22,
                fontSize: 28,
                letterSpacing: 8,
                color: SMOKE,
              }}
            >
              AN AI PLAYS. FOREVER. NO CHEATS.
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between" }}>
            {row ? (
              <div style={{ display: "flex" }}>
                <Counter value={formatInt(row.deaths)} label="WASTED" loud />
                <Counter value={formatInt(row.busted)} label="BUSTED" loud />
                <Counter value={formatInt(row.missions_passed)} label="MISSIONS" />
              </div>
            ) : (
              <div style={{ display: "flex", fontSize: 26, color: SMOKE, maxWidth: 620 }}>
                the agent is an AI. He drives, he crashes, he explains himself.
              </div>
            )}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                border: `2px solid ${offline ? ASH : EMBER}`,
                padding: "10px 18px",
                fontSize: 24,
                letterSpacing: 5,
                color: offline ? SMOKE : EMBER,
              }}
            >
              {statusText}
            </div>
          </div>
        </div>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            padding: "0 64px 26px",
            fontSize: 19,
            letterSpacing: 4,
            color: SMOKE,
          }}
        >
          <div style={{ display: "flex" }}>COMMENTARY IS AI-GENERATED</div>
          <div style={{ display: "flex" }}>NOT AFFILIATED WITH ANY GAME PUBLISHER</div>
        </div>
        <div style={{ display: "flex", height: 18, width: "100%", backgroundColor: BLOOD }} />
      </div>
    ),
    { ...size, headers: { "cache-control": CARD_CACHE_CONTROL } }
  );
}
