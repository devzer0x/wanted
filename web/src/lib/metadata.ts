import type { Metadata } from "next";

// Per-route share metadata.
//
// Next.js does NOT deep-merge `openGraph` or `alternates` across segments: a leaf's value
// replaces the ancestor's wholesale, and a leaf that omits the key inherits the ancestor's value
// verbatim (next/dist/lib/metadata/resolve-metadata.js — `case 'openGraph'` assigns, it does not
// spread). So declaring `openGraph.url` / `alternates.canonical` only in the root layout makes
// every other route advertise the homepage's og:title, the bare origin as og:url, and the origin
// as its canonical. Clip permalinks are a share surface with their own OG image, so that is a
// real bug, not cosmetics.
//
// Every route therefore builds its own block through `routeMetadata`, and the root layout
// deliberately declares no `url` and no `canonical` — so the 404 boundary (which resolves with
// the root's metadata only) claims neither.

export const SITE_NAME = "WANTED";
export const SITE_OG_ALT = "WANTED — an AI plays GTA. Predict what happens next.";
export const SITE_TITLE = "WANTED — an AI plays GTA. Predict what happens next.";
export const TITLE_TEMPLATE = "%s · WANTED";
export const SITE_DESCRIPTION =
  "An AI plays a famous open-world story mode 24/7, live. Predict and Earn $TTWO — free to enter, nothing to lose by being wrong. All commentary and gameplay decisions are AI-generated.";

/**
 * The document title a page ends up with, resolved here rather than left to Next's template.
 * `openGraph.title` gets its own template chain, so passing the finished string keeps og:title
 * and <title> provably identical instead of accidentally diverging.
 */
export function formatTitle(title?: string): string {
  return title ? TITLE_TEMPLATE.replace("%s", title) : SITE_TITLE;
}

/**
 * The site-wide share card, re-attached explicitly by `routeMetadata`.
 *
 * Next merges an `opengraph-image` file into `openGraph.images` at the segment that owns the
 * file, and only when that segment's own metadata does not already declare `images`. Because a
 * route declaring `openGraph` replaces the root layout's resolved object outright, the root card
 * would otherwise vanish from every route that sets its own og:url — so it is named here instead
 * of relied on by inheritance. `/opengraph-image` is a real route (app/opengraph-image.tsx); the
 * card is regenerated per request from live stats behind its own CDN cache header, so the
 * content-hash query Next appends buys nothing here.
 */
export const SITE_OG_IMAGE = {
  url: "/opengraph-image",
  width: 1200,
  height: 630,
  alt: SITE_OG_ALT,
} as const;

export interface RouteMetadataInput {
  /** Origin-relative route path, e.g. "/missions" or "/clips/42". Resolved against metadataBase. */
  path: string;
  /** Short page title. Omitted on the home page, which uses the site title verbatim. */
  title?: string;
  description?: string;
  /**
   * True for a route that ships its own `opengraph-image` file (currently `/clips/[id]`).
   * Declaring `openGraph.images` here would make Next skip that file, so the site card is left
   * off and the route's own card is merged in instead.
   */
  hasOwnOgImage?: boolean;
}

export function routeMetadata({
  path,
  title,
  description,
  hasOwnOgImage = false,
}: RouteMetadataInput): Metadata {
  const ogTitle = formatTitle(title);
  const desc = description ?? SITE_DESCRIPTION;
  return {
    // Omitted on the home page so the root layout's `title.default` applies unchanged.
    ...(title ? { title } : {}),
    description: desc,
    alternates: { canonical: path },
    openGraph: {
      type: "website",
      siteName: SITE_NAME,
      url: path,
      title: ogTitle,
      description: desc,
      ...(hasOwnOgImage ? {} : { images: [SITE_OG_IMAGE] }),
    },
    twitter: {
      card: "summary_large_image",
      title: ogTitle,
      description: desc,
    },
  };
}
