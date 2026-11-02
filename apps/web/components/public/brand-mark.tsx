import { Lock } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Brand mark for the public-facing pages (apply + shortlist). Falls back
 * to the generic "R" tile when the org hasn't uploaded a logo yet, so the
 * surface always looks finished even on a fresh org.
 *
 * `logoUrl` is the *absolute* URL the browser will load — the caller is
 * responsible for prepending API_BASE_URL to the org's relative
 * `logo_path`. Keeping that out of this component lets us avoid leaking
 * the API URL into the client bundle.
 */
export function BrandMark({
  logoUrl,
  accentColor,
  orgName,
  confidential = false,
  className,
}: {
  logoUrl: string | null;
  accentColor: string | null;
  orgName: string;
  /** Withheld employer — render a neutral lock instead of a monogram, so the
   *  placeholder label's first letter isn't mistaken for a company initial. */
  confidential?: boolean;
  className?: string;
}) {
  if (confidential) {
    return (
      <span className={cn("flex items-center gap-2", className)}>
        <span className="flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-md border border-border bg-bg-elevated text-text-faint">
          <Lock className="h-3 w-3" />
        </span>
      </span>
    );
  }
  if (logoUrl) {
    return (
      <span
        className={cn(
          "flex h-7 items-center gap-2.5",
          className,
        )}
      >
        <img
          src={logoUrl}
          alt={orgName}
          referrerPolicy="no-referrer"
          className="h-7 w-auto max-w-[120px] rounded-[4px] object-contain"
        />
      </span>
    );
  }
  // Fallback: monochrome "R" tile, accent dot uses the brand color when set.
  return (
    <span className={cn("flex items-center gap-2", className)}>
      <span className="relative flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-md bg-text font-bold text-bg text-[11px] tracking-tight">
        {firstLetter(orgName)}
        <span
          className="absolute -right-[3px] -top-[3px] h-1.5 w-1.5 rounded-full"
          style={{ background: accentColor ?? "var(--primary)" }}
        />
      </span>
    </span>
  );
}

function firstLetter(name: string): string {
  const s = name.trim();
  if (!s) return "·";
  // Use the first non-whitespace grapheme. For Cyrillic names this is
  // fine; for emoji prefixes we fall back to "·".
  const code = s.codePointAt(0);
  if (code == null) return "·";
  const ch = String.fromCodePoint(code);
  return /\p{L}|\p{N}/u.test(ch) ? ch.toUpperCase() : "·";
}
