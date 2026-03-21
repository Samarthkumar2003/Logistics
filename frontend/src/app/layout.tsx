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
    <html lang="en">
      <body>
        {children}
      </body>
    </html>
  );
}
