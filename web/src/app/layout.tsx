import type { Metadata, Viewport } from "next";
import { Archivo, Archivo_Black, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";
import { SiteNav } from "@/components/SiteNav";
import { SiteFooter } from "@/components/SiteFooter";

const archivo = Archivo({
  variable: "--font-archivo",
  subsets: ["latin"],
});

const archivoBlack = Archivo_Black({
  variable: "--font-archivo-black",
  weight: "400",
  subsets: ["latin"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  weight: ["400", "600"],
  subsets: ["latin"],
});

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3000";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: "WANTED — the agent plays. Forever.",
    template: "%s · WASTED",
  },
  description:
    "An AI called the agent plays a famous open-world story mode 24/7, live. No cheats, no god mode — just a robot, a car, and bad decisions. All commentary is AI-generated.",
};

export const viewport: Viewport = {
  themeColor: "#070606",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${archivo.variable} ${archivoBlack.variable} ${plexMono.variable} grain min-h-dvh antialiased flex flex-col`}
      >
        <SiteNav />
        <main className="flex-1 w-full max-w-6xl mx-auto px-3 sm:px-5 pb-16">
          {children}
        </main>
        <SiteFooter />
      </body>
    </html>
  );
}
