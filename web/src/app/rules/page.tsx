import type { Metadata } from "next";

import { RULES_VERSION } from "@/lib/auth/siwe";
import { explorerAddressUrl } from "@/lib/chain/config";
import { routeMetadata } from "@/lib/metadata";

export const metadata: Metadata = routeMetadata({
  path: "/rules",
  title: "Rules",
  description: "How WANTED predictions, rewards and claims work — and who can receive TTWO.",
});

// The restricted list is read from the SAME variable policy enforces (POLICY_BLOCKED_REGIONS), so
// this page cannot say one thing while the server does another. Dynamic for the same reason: the
// list is a deployment setting, and a page baked at build time could go stale against it.
export const dynamic = "force-dynamic";

const EFFECTIVE = "14 September 2026";

function blockedRegions(): string[] {
  const raw = process.env.POLICY_BLOCKED_REGIONS?.trim();
  if (!raw) return [];
  const names = new Intl.DisplayNames(["en"], { type: "region" });
  return raw
    .split(",")
    .map((c) => c.trim().toUpperCase())
    .filter((c) => /^[A-Z]{2}$/.test(c))
    .map((c) => names.of(c) ?? c);
}

function treasuryAddress(): string | null {
  const raw = process.env.NEXT_PUBLIC_TREASURY_ADDRESS?.trim();
  return raw && /^0x[0-9a-fA-F]{40}$/.test(raw) ? raw : null;
}

function Section({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <section className="panel overflow-hidden">
      <h2 className="panel-title flex items-center gap-2.5 border-b-[3px] border-ink bg-sand px-4 py-2.5 text-[17px]">
        <span className="grid size-7 place-items-center rounded-full border-[3px] border-ink bg-yellow text-[13px]">
          {n}
        </span>
        {title}
      </h2>
      <div className="flex flex-col gap-2.5 px-4 py-3.5 text-[14px] leading-[1.6] font-semibold text-ink">
        {children}
      </div>
    </section>
  );
}

export default function RulesPage() {
  const regions = blockedRegions();
  const treasury = treasuryAddress();

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 py-8">
      <header className="flex flex-col items-start gap-3">
        <h1 className="page-title bg-coral text-white">Rules</h1>
        <p className="text-[13px] font-extrabold tracking-[0.06em] text-muted uppercase">
          Version {RULES_VERSION} · effective {EFFECTIVE}
        </p>
        <p className="text-[15px] leading-[1.55] font-bold">
          Signing in with your wallet means you accept this version. When it changes in substance
          the version number goes up, and you will need to sign in again before you can claim.
        </p>
      </header>

      <Section n={1} title="The short version">
        <p>
          Predicting is free. There is nothing to buy, nothing to deposit and nothing to lose by
          being wrong. If you are right, you may earn a share of a small reward paid in TTWO, where
          rewards are available to you.
        </p>
      </Section>

      <Section n={2} title="What you are predicting">
        <p>
          An AI agent plays Grand Theft Auto V story mode live. Each prediction asks what it will do
          in a short window. Entries close before the window starts, and every prediction is settled
          automatically from the game&apos;s own telemetry — not by a person.
        </p>
        <p>
          A prediction is voided, and pays nothing, if nobody entered, if the telemetry for its
          window is incomplete, or if the stream ends during it.
        </p>
      </Section>

      <Section n={3} title="How rewards are shared">
        <p>
          Each prediction shows its reward pool. When it settles, the pool is split equally between
          every wallet that picked the correct outcome. Any remainder too small to split stays in
          the treasury.
        </p>
        <p>
          Daily and per-wallet limits apply. When a limit is reached, a credit can be reduced or not
          issued. Credits appear in your balance when the prediction settles.
        </p>
      </Section>

      <Section n={4} title="Claiming">
        <p>
          Rewards are paid to the wallet you signed in with, on Robinhood Chain (chain id 4663).
          Claims are processed automatically and are usually sent within a few minutes. A balance
          below the minimum claim amount waits until it reaches the minimum.
        </p>
        <p>
          If a transfer fails, the credits go back into your balance and you can claim again.
          {treasury ? (
            <>
              {" "}
              Rewards are paid from the WANTED treasury wallet{" "}
              <a
                href={explorerAddressUrl(treasury)}
                className="font-mono text-[12.5px] break-all underline"
                target="_blank"
                rel="noreferrer"
              >
                {treasury}
              </a>
              , which anyone can inspect on the block explorer.
            </>
          ) : null}
        </p>
      </Section>

      <Section n={5} title="Who can receive rewards">
        <p>
          TTWO is a tokenized debt security issued by Robinhood Assets (Jersey) Limited that tracks
          the share price of Take-Two Interactive. It is not issued by Take-Two, it gives you no
          shareholder rights, and its use here implies no relationship with, or endorsement by,
          Take-Two or Robinhood.
        </p>
        <p>
          It is restricted or prohibited in several jurisdictions.{" "}
          {regions.length > 0 ? (
            <>
              Rewards are not available to anyone located in{" "}
              <strong>{regions.join(", ")}</strong>.
            </>
          ) : (
            <>No locations are excluded by this deployment at the moment.</>
          )}{" "}
          Your location is determined from your internet connection when you check your balance or
          claim. You must not use a VPN or any other means to appear to be somewhere else in order
          to receive rewards, and you must be old enough to hold crypto assets where you live.
        </p>
        <p>You can still predict, and appear on the leaderboard, from anywhere.</p>
      </Section>

      <Section n={6} title="Fair play">
        <p>
          One person, one wallet. Running several wallets to multiply rewards, automating entries,
          or exploiting a bug is not allowed. Wallets involved can be excluded from rewards, and
          their unclaimed credits withheld.
        </p>
      </Section>

      <Section n={7} title="Rewards can change or stop">
        <p>
          Rewards are funded by the operator and can be paused, reduced or ended at any time,
          including for a single prediction. While rewards are paused, correct predictions still
          count for streaks and the leaderboard but earn no credits, and those credits are not paid
          later. Pausing rewards can also pause claims.
        </p>
      </Section>

      <Section n={8} title="Not advice">
        <p>
          Nothing on WANTED is investment, legal or tax advice. You are responsible for any tax due
          on rewards you receive. The agent is an AI, and all commentary on this site is
          AI-generated.
        </p>
      </Section>
    </div>
  );
}
