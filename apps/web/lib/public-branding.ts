const LOGO_SUFFIX = /\.(?:png|jpe?g|webp)$/i;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function logoSegments(value: string): string[] | null {
  const withoutQuery = value.split(/[?#]/, 1)[0];
  const normalized = withoutQuery
    .replace(/^\/+/, "")
    .replace(/^storage\//, "")
    .replace(/^logos\//, "");
  const segments = normalized.split("/").filter(Boolean);
  if (segments.some((segment) => segment === "." || segment === "..")) return null;
  if (segments.length === 1) {
    const stem = segments[0].replace(LOGO_SUFFIX, "");
    return LOGO_SUFFIX.test(segments[0]) && UUID.test(stem) ? segments : null;
  }
  if (segments.length === 2) {
    const stem = segments[1].replace(LOGO_SUFFIX, "");
    return UUID.test(segments[0]) && LOGO_SUFFIX.test(segments[1]) && UUID.test(stem)
      ? segments
      : null;
  }
  return null;
}

/**
 * Convert API branding references into a browser-safe URL.
 *
 * Absolute HTTPS CDN URLs stay direct. Historical `/storage/logos/...` and
 * raw `logos/...` keys go through the same-origin BFF so an internal
 * `API_BASE_URL` (for example `http://api:8000`) never reaches browser HTML.
 */
export function publicBrandingUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const absolute = new URL(value);
    if (absolute.protocol === "https:") return absolute.toString();
    if (
      process.env.NODE_ENV !== "production" &&
      absolute.protocol === "http:" &&
      ["localhost", "127.0.0.1", "::1"].includes(absolute.hostname)
    ) {
      return absolute.toString();
    }
    return null;
  } catch {
    const segments = logoSegments(value);
    return segments
      ? `/api/public/branding/${segments.map(encodeURIComponent).join("/")}`
      : null;
  }
}

export function validBrandingProxySegments(segments: string[]): boolean {
  return logoSegments(segments.join("/")) !== null;
}
