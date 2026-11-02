/**
 * Tight relative-time helpers. We reach for these in the chrome (dates,
 * activity strings) where readability matters more than precision.
 */
export function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const seconds = Math.max(0, Math.floor(diff / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 14) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 8) return `${weeks}w ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  const years = Math.floor(days / 365);
  return `${years}y ago`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function prjRef(id: string): string {
  // Stable short reference for a position UUID, e.g. PRJ-A1B2C3
  return `PRJ-${id.replace(/-/g, "").slice(0, 6).toUpperCase()}`;
}

export function candRef(id: string, sourceRef?: string | null): string {
  if (sourceRef && sourceRef.trim()) return sourceRef;
  return `cand_${id.replace(/-/g, "").slice(0, 8)}`;
}

/**
 * Compact ref for the candidates list. Strips a trailing phone number so the
 * row shows "Иван Петров" instead of "Иван Петров +992 90 123 4567". The full
 * source_ref still flows into the row's `title` for hover-tooltip + the
 * candidate detail page renders contact info in its own section anyway.
 *
 * Matches: an optional +, then >=7 digits possibly separated by spaces, -,
 * or parens, at the end of the string.
 */
export function candDisplayRef(id: string, sourceRef?: string | null): string {
  if (!sourceRef || !sourceRef.trim()) {
    return `cand_${id.replace(/-/g, "").slice(0, 8)}`;
  }
  const trimmed = sourceRef
    .replace(/\s*\+?[\d][\d\s\-()]{6,}\s*$/u, "")
    .trim();
  return trimmed || sourceRef;
}
