"use client";

import { useEffect, useState } from "react";
import type { StreamConfig } from "@/lib/types";

// Twitch requires `parent` = the exact hostname the player is embedded in, and that hostname
// differs per deployment (vercel.app preview, wasted-lemon.vercel.app, a future custom domain,
// localhost in dev). It is therefore read from window.location at runtime rather than baked into
// the build (D8) — which is also why the iframe renders only after mount. Autoplay must be muted
// or browsers block it.
function twitchSrc(channel: string, host: string): string {
  const q = new URLSearchParams({
    channel,
    parent: host,
    muted: "true",
    autoplay: "true",
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
  useEffect(() => {
    setHost(window.location.hostname);
  }, []);

  let inner: React.ReactNode;
  let configured = true;

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
      inner = (
        <iframe
          data-testid="stream-player"
          src={twitchSrc(config.channel, host)}
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
