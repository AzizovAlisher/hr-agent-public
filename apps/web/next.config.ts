import type { NextConfig } from "next";

function strongProductionSecret(value: string | undefined): value is string {
  return Boolean(
    value &&
      value.length >= 43 &&
      new Set(value).size >= 16 &&
      !/change[-_ ]?me|replace[-_ ]?me|example/i.test(value),
  );
}

if (process.env.NODE_ENV === "production") {
  const authJsSecret = process.env.AUTH_SECRET ?? process.env.NEXTAUTH_SECRET;
  const sessionSecret = process.env.AUTH_SESSION_SECRET;
  if (!strongProductionSecret(authJsSecret)) {
    throw new Error(
      "Production requires a high-entropy AUTH_SECRET or NEXTAUTH_SECRET (at least 32 random bytes)",
    );
  }
  if (
    !strongProductionSecret(sessionSecret) ||
    sessionSecret === authJsSecret
  ) {
    throw new Error(
      "Production requires an independent, high-entropy AUTH_SESSION_SECRET (at least 32 random bytes)",
    );
  }
}

// Candidate CVs remain capped at 15 MiB by the API, while durable sourcing
// imports accept 25 MiB. Multipart framing needs a little transport headroom;
// each API endpoint still enforces its narrower content-specific limit.
const UPLOAD_REQUEST_LIMIT = "26mb";

const SECURITY_HEADERS = [
  {
    key: "Content-Security-Policy",
    value: "base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'",
  },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Permitted-Cross-Domain-Policies", value: "none" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  {
    key: "Permissions-Policy",
    value:
      "camera=(), geolocation=(), microphone=(), payment=(), usb=(), browsing-topics=()",
  },
  ...(process.env.NODE_ENV === "production"
    ? [
        {
          key: "Strict-Transport-Security",
          value: "max-age=63072000; includeSubDomains; preload",
        },
      ]
    : []),
];

const PRIVATE_TOKEN_HEADERS = [
  { key: "Cache-Control", value: "private, no-store, max-age=0" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Robots-Tag", value: "noindex, nofollow, noarchive" },
];

const PRIVATE_AI_HEADERS = [
  {
    key: "Cache-Control",
    value: "private, no-store, max-age=0, no-transform",
  },
  { key: "Pragma", value: "no-cache" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Robots-Tag", value: "noindex, nofollow, noarchive" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [
      { source: "/:path*", headers: SECURITY_HEADERS },
      { source: "/r/:path*", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/shortlist/:path*", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/schedule/:path*", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/offer/:path*", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/assessment/:path*", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/invite/:path*", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/reset-password/:path*", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/forgot-password", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/integrations/oauth/:path*", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/apply/:path*", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/access-gone", headers: PRIVATE_TOKEN_HEADERS },
      { source: "/login", headers: PRIVATE_TOKEN_HEADERS },
      {
        source: "/api/auth/:path*",
        headers: [
          { key: "Cache-Control", value: "private, no-store, max-age=0" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
      {
        source: "/api/auth/local-logout",
        headers: [
          {
            key: "Content-Security-Policy",
            value:
              "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
          },
          { key: "Cache-Control", value: "private, no-store, max-age=0" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Robots-Tag", value: "noindex, nofollow, noarchive" },
        ],
      },
      {
        source: "/api/assessment/:path*",
        headers: [
          { key: "Cache-Control", value: "private, no-store, max-age=0" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
      {
        source: "/api/chat/:path*",
        headers: [{ key: "Cache-Control", value: "private, no-store" }],
      },
      {
        source: "/api/ai/:path*",
        headers: PRIVATE_AI_HEADERS,
      },
      {
        source: "/api/candidates/:path*",
        headers: PRIVATE_TOKEN_HEADERS,
      },
      {
        source: "/api/enterprise/:path*",
        headers: PRIVATE_TOKEN_HEADERS,
      },
    ];
  },
  experimental: {
    // Auth runs through proxy/middleware, which clones and buffers request
    // bodies. Its default 10 MB cap was truncating larger CV uploads and
    // causing React's multipart parser to throw "Unexpected end of form".
    proxyClientMaxBodySize: UPLOAD_REQUEST_LIMIT,
    serverActions: {
      bodySizeLimit: UPLOAD_REQUEST_LIMIT,
    },
  },
};

export default nextConfig;
