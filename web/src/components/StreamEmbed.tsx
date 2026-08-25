"use client";

import { useEffect, useState } from "react";
import type { StreamConfig } from "@/lib/types";

// Twitch requires `parent` = the embedding hostname; preview/prod hostnames differ per deploy,
// so it is computed at runtime from window.location (D8). Rendered only after mount to avoid a
// server/client hostname mismatch. Autoplay must be muted or browsers block it.
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

function Unconfigured({ note }: { note: string }) {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-3 p-6 text-center">
      <span className="wordmark text-4xl sm:text-5xl" data-text="NO SIGNAL">
        NO SIGNAL
      </span>
      <p className="ticker text-[0.68rem] text-smoke max-w-sm">{note}</p>
    </div>
  );
}

export function StreamEmbed({ config }: { config: StreamConfig | null }) {
  const [host, setHost] = useState<string | null>(null);
  useEffect(() => {
    setHost(window.location.hostname);
  }, []);

  let inner: React.ReactNode;
  if (!config) {
    inner = <Unconfigured note="Stream source not configured. The show is off the air." />;
  } else if (config.provider === "twitch") {
    if (!config.channel) {
      inner = <Unconfigured note="Twitch source configured without a channel." />;
    } else if (!host) {
      inner = <div className="h-full w-full bg-void" aria-hidden="true" />;
    } else {
      inner = (
        <iframe
          src={twitchSrc(config.channel, host)}
          title={`Live stream: ${config.channel}`}
          className="h-full w-full"
          allowFullScreen
          allow="autoplay; fullscreen"
        />
      );
    }
  } else if (!config.video_id) {
    inner = (
      <Unconfigured note="YouTube source configured without a video id. Waiting for the live video id." />
    );
  } else {
    inner = (
      <iframe
        src={youtubeSrc(config.video_id)}
        title="Live stream"
        className="h-full w-full"
        allowFullScreen
        allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
      />
    );
  }

  return (
    <div className="panel relative aspect-video w-full overflow-hidden">
      {inner}
    </div>
  );
}
