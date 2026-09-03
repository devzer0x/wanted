"use client";

import { useEffect, useState } from "react";
import type { StreamConfig } from "@/lib/types";

// Twitch requires `parent` = the exact hostname the player is embedded in, and that hostname
// differs per deployment (a Vercel preview URL, the production domain,
// localhost in dev). It is therefore read from window.location at runtime rather than baked into
// the build (D8) — which is also why the iframe renders only after mount. Autoplay must be muted
// or browsers block it.
//
// `controls: false` is Twitch's own documented embed parameter and it is what makes the stream
// read as OUR stream: the player's control bar is the only surface that carries Twitch chrome
// (the logo, the channel links, "watch on Twitch"), and it lives inside a cross-origin iframe
// where our CSS cannot reach it. Turning the bar off is therefore the only way to remove it.
//
// Losing the bar also loses the only volume control, so the panel supplies its own unmute
// (see `StreamEmbed`): swapping `muted` re-mounts the iframe, and because that swap happens
// inside a click handler the browser treats it as a user gesture and allows sound.
function twitchSrc(channel: string, host: string, muted: boolean): string {
  const q = new URLSearchParams({
    channel,
    parent: host,
    muted: muted ? "true" : "false",
    autoplay: "true",
    controls: "false",
  });
  return `https://player.twitch.tv/?${q.toString()}`;
}

function youtubeSrc(videoId: string): string {
  const q = new URLSearchParams({ autoplay: "1", mute: "1", playsinline: "1" });
  return `https://www.youtube.com/embed/${encodeURIComponent(videoId)}?${q.toString()}`;
}

function NoSignal({ note }: { note: string }) {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-3 px-5 py-6 text-center">
      <div className="stripes-dim h-1 w-24" aria-hidden="true" />
      <span className="wordmark text-3xl text-bone sm:text-5xl" data-text="NO SIGNAL">
        NO SIGNAL
      </span>
      <p className="max-w-sm text-xs leading-relaxed text-smoke">{note}</p>
      <div className="stripes-dim h-1 w-24" aria-hidden="true" />
    </div>
  );
}

type Status = "no-signal" | "off-air" | "live";

const STATUS_LABEL: Record<Status, string> = {
  "no-signal": "no signal",
  "off-air": "off air",
  live: "live",
};

function StatusChip({ status }: { status: Status }) {
  const live = status === "live";
  return (
    <span
      data-testid="stream-status"
      data-status={status}
      className={`ticker flex items-center gap-1.5 border px-1.5 py-0.5 text-[0.55rem] ${
        live ? "border-blood text-ember" : "border-ash text-smoke"
      }`}
    >
      <span className={`led ${live ? "led-live" : "led-dead"}`} aria-hidden="true" />
      {STATUS_LABEL[status]}
    </span>
  );
}

/**
 * Three honest states, per CONTRACTS §5 v1.1:
 *  - no `site_config` "stream" row and no env fallback → "no signal" panel, no player;
 *  - configured but the agent's heartbeat is stale → the player is embedded and the panel says
 *    off air, so a Twitch/YouTube offline screen is never mistaken for a broken site;
 *  - configured and the heartbeat is fresh → the player with a live indicator.
 * `offline` is the heartbeat-derived signal (CONTRACTS §5), never a guess about the channel.
 */
export function StreamEmbed({
  config,
  offline,
}: {
  config: StreamConfig | null;
  offline: boolean;
}) {
  const [host, setHost] = useState<string | null>(null);
  // Muted is the only state autoplay is allowed to start in; the viewer opts into sound.
  const [muted, setMuted] = useState(true);
  useEffect(() => {
    setHost(window.location.hostname);
  }, []);

  let inner: React.ReactNode;
  let configured = true;
  // Only the Twitch branch has its controls turned off, so only it needs our sound toggle.
  let isTwitch = false;

  if (!config) {
    configured = false;
    inner = (
      <NoSignal note="No stream source is configured yet. When the channel goes up it appears here — until then there is nothing to show, so we show nothing." />
    );
  } else if (config.provider === "twitch") {
    if (!config.channel) {
      configured = false;
      inner = <NoSignal note="A Twitch source is configured without a channel name." />;
    } else if (!host) {
      // Pre-mount: the parent hostname is not known yet, so no player can be built.
      inner = <div className="h-full w-full bg-void" aria-hidden="true" />;
    } else {
      isTwitch = true;
      inner = (
        <iframe
          // Keyed on `muted` so React remounts the iframe on the toggle rather than
          // mutating a live player's src, which Twitch does not reliably honour.
          key={muted ? "muted" : "unmuted"}
          data-testid="stream-player"
          src={twitchSrc(config.channel, host, muted)}
          title={`Live stream: ${config.channel}`}
          className="h-full w-full"
          // A cross-origin document that fails to load (blocked player, flaky network) paints
          // the browser's default canvas; color-scheme keeps that dark instead of a white flash.
          style={{ colorScheme: "dark" }}
          allowFullScreen
          allow="autoplay; fullscreen"
        />
      );
    }
  } else if (!config.video_id) {
    configured = false;
    inner = (
      <NoSignal note="A YouTube source is configured without a video id. Waiting for the live video id." />
    );
  } else {
    inner = (
      <iframe
        data-testid="stream-player"
        src={youtubeSrc(config.video_id)}
        title="Live stream"
        className="h-full w-full"
        style={{ colorScheme: "dark" }}
        allowFullScreen
        allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
      />
    );
  }

  const status: Status = !configured ? "no-signal" : offline ? "off-air" : "live";

  return (
    <section className="panel overflow-hidden" aria-label="Live stream">
      <div className="flex items-center justify-between gap-2 border-b border-ash px-3 py-2">
        <h2 className="panel-title">The stream</h2>
        <StatusChip status={status} />
      </div>
      {/* A 16:9 box is reserved only when there is a player to put in it; an unconfigured source
          gets a compact panel instead of a screen-height rectangle of black. */}
      <div
        className={`relative w-full bg-void ${
          configured ? "aspect-video" : "min-h-[13rem] sm:min-h-[16rem]"
        }`}
      >
        {inner}
        {/* The Twitch control bar is off (see `twitchSrc`), so sound is ours to offer.
            Rendered only over a real player, never over the NO SIGNAL panel. */}
        {configured && isTwitch && (
          <button
            type="button"
            data-testid="stream-sound"
            onClick={() => setMuted((m) => !m)}
            aria-pressed={!muted}
            className="ticker absolute bottom-2 right-2 border border-ash bg-void/80 px-2 py-1 text-[0.55rem] text-smoke transition-colors hover:border-blood hover:text-ember"
          >
            {muted ? "sound off" : "sound on"}
          </button>
        )}
      </div>
      {status === "off-air" && (
        <p className="border-t border-ash px-3 py-2 text-[0.62rem] leading-snug text-smoke">
          The rig is not sending a heartbeat, so the player above may show the channel&apos;s own
          offline screen. Nothing here is a replay.
        </p>
      )}
    </section>
  );
}
