import "./globals.css";

export const metadata = {
  title: "Logistics Copilot - Multi-Agent Office",
  description: "Visual interface for AI operations",
};

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
        {children}
      </body>
    </html>
  );
}
