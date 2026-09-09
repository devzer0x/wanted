import type { Metadata, Viewport } from "next";
import { Lilita_One, Nunito } from "next/font/google";
import "./globals.css";
import { SiteNav } from "@/components/SiteNav";
import { SiteFooter } from "@/components/SiteFooter";
import { WalletProvider } from "@/components/wallet/WalletProvider";
import { getSiteUrl } from "@/lib/env";
import { SITE_DESCRIPTION, SITE_NAME, SITE_TITLE, TITLE_TEMPLATE } from "@/lib/metadata";

// Display type. One weight only — Lilita One ships a single face, which is why the design leans
// on size and colour for hierarchy rather than weight.
const lilita = Lilita_One({
  variable: "--font-lilita",
  weight: "400",
  subsets: ["latin"],
  display: "swap",
});

// Body. The design sets almost everything at 700+, so the light weights are not loaded.
const nunito = Nunito({
  variable: "--font-nunito",
  weight: ["600", "700", "800", "900"],
  subsets: ["latin"],
  display: "swap",
});

const siteUrl = getSiteUrl();

// Root-level metadata carries only what is TRUE for every route: the origin, the title template,
// the site description and the site-wide share card. It deliberately declares no
// `alternates.canonical` and no `openGraph.url`, because Next replaces (never merges) those keys
// per segment — an ancestor value silently becomes every descendant's answer, and every route
// would then point crawlers at the homepage. Each route supplies its own via
// `routeMetadata` (src/lib/metadata.ts); the 404 boundary, which resolves against this object
// alone, correctly ends up with neither.
export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: SITE_TITLE,
    template: TITLE_TEMPLATE,
  },
  description: SITE_DESCRIPTION,
  applicationName: SITE_NAME,
  openGraph: {
    type: "website",
    siteName: SITE_NAME,
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
  },
  twitter: {
    card: "summary_large_image",
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
  },
};

export const viewport: Viewport = {
  themeColor: "#070606",
  colorScheme: "dark",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${lilita.variable} ${nunito.variable}`}
      >
        <a
          href="#content"
          className="ticker sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-100 focus:border focus:border-blood focus:bg-void focus:px-3 focus:py-2 focus:text-[0.65rem] focus:text-ember"
        >
          Skip to content
        </a>
        <WalletProvider>
          <SiteNav />
          <main id="content" className="flex-1 w-full max-w-6xl mx-auto px-3 sm:px-5 pb-16">
            {children}
          </main>
          <SiteFooter />
        </WalletProvider>
      </body>
    </html>
  );
}
