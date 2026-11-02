import type { Metadata } from "next";
import { Geist, Geist_Mono, Inter } from "next/font/google";

import { ThemeProvider } from "@/components/theme-provider";
import { Toaster } from "@/components/ui/sonner";
import { LocaleProvider } from "@/components/i18n/locale-provider";
import { getLocale } from "@/lib/i18n";
import "./globals.css";

// Geist for Latin (no Cyrillic glyphs); Inter as Cyrillic fallback so the
// stack picks Geist where it can and Inter where it must.
const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});
const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});
const interFallback = Inter({
  variable: "--font-cyrillic",
  subsets: ["latin", "cyrillic"],
});

export const metadata: Metadata = {
  title: "Recruiter",
  description: "B2B recruitment, ranking, and chat over candidates.",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const locale = await getLocale();
  return (
    <html
      lang={locale}
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} ${interFallback.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <ThemeProvider
          attribute="class"
          defaultTheme="dark"
          enableSystem
          disableTransitionOnChange
        >
          <LocaleProvider initialLocale={locale}>
            <a className="skip-link" href="#main-content">
              {locale === "ru" ? "Перейти к содержимому" : "Skip to main content"}
            </a>
            <div
              id="main-content"
              tabIndex={-1}
              className="flex min-h-0 flex-1 flex-col outline-none"
            >
              {children}
            </div>
            <Toaster richColors position="top-right" />
          </LocaleProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
