import type { Metadata, Viewport } from "next";

import "./globals.css";
import "./visual-system.css";

export const metadata: Metadata = {
  title: "TalosAI Workspace",
  description: "Local, developer-first AI agent workspace",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#151c19" },
    { media: "(prefers-color-scheme: light)", color: "#f4f5ef" },
  ],
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}