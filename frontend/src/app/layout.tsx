import "./globals.css";
// Light/dark tokens for every themed route, not just the dashboard. Imported
// here rather than per-page so a page cannot load its markup without them.
import "./theme.css";
import Script from "next/script";

import AuthGate from "@/components/AuthGate";

export const metadata = {
  title: "Logistics Copilot - Multi-Agent Office",
  description: "Visual interface for AI operations",
};

// Applied before paint so a stored light theme never flashes dark first. Dark is
// the default. `beforeInteractive` must live in the root layout — it is injected
// into <head> and runs before hydration, replacing the old inline <script> in the
// dashboard layout (which React 19 refuses to execute on the client).
const THEME_INIT = `(function(){try{var t=localStorage.getItem('dashboard-theme');document.documentElement.dataset.theme=t==='light'?'light':'dark';}catch(e){document.documentElement.dataset.theme='dark';}})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // The dashboard's theme script stamps data-theme on <html> before React
    // hydrates, so this element's attributes legitimately differ from the SSR
    // output. Suppression applies to this element only, not its subtree.
    <html lang="en" suppressHydrationWarning>
      <body>
        <Script id="theme-init" strategy="beforeInteractive">
          {THEME_INIT}
        </Script>
        {/*
          Wrapped here, in the root layout, so there is no page in the app that
          can forget to gate itself. AuthGate is a client component; this layout
          stays a server component, because a client child does not make its
          parent one. It exempts /login itself — see the component.
        */}
        <AuthGate>{children}</AuthGate>
      </body>
    </html>
  );
}
