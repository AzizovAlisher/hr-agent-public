import { type NextRequest, NextResponse } from "next/server";

import { validBrandingProxySegments } from "@/lib/public-branding";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

/** @local-only Next.js catch-all route context; it is never serialized. */
type RouteContext = { params: Promise<{ path: string[] }> };

function publicRedirect(location: string): URL | null {
  try {
    const target = new URL(location);
    if (target.protocol === "https:") return target;
    if (
      process.env.NODE_ENV !== "production" &&
      target.protocol === "http:" &&
      ["localhost", "127.0.0.1", "::1"].includes(target.hostname)
    ) {
      return target;
    }
  } catch {
    // The storage service must emit an absolute public URL.
  }
  return null;
}

async function brandingResponse(
  request: NextRequest,
  { params }: RouteContext,
): Promise<NextResponse> {
  const { path } = await params;
  if (!validBrandingProxySegments(path)) {
    return new NextResponse(null, {
      status: 404,
      headers: { "Cache-Control": "public, max-age=60" },
    });
  }

  const key = path.map(encodeURIComponent).join("/");
  let upstream: Response;
  try {
    upstream = await fetch(
      `${API_BASE_URL.replace(/\/$/, "")}/storage/logos/${key}`,
      {
        method: request.method,
        cache: "no-store",
        redirect: "manual",
      },
    );
  } catch {
    return new NextResponse(null, {
      status: 503,
      headers: { "Cache-Control": "no-store", "Retry-After": "30" },
    });
  }

  if ([301, 302, 303, 307, 308].includes(upstream.status)) {
    const target = publicRedirect(upstream.headers.get("location") ?? "");
    if (!target) {
      return new NextResponse(null, {
        status: 502,
        headers: { "Cache-Control": "no-store" },
      });
    }
    const response = NextResponse.redirect(target, 307);
    response.headers.set("Cache-Control", "public, max-age=300");
    response.headers.set("X-Content-Type-Options", "nosniff");
    return response;
  }

  if (!upstream.ok) {
    return new NextResponse(null, {
      status: upstream.status === 404 ? 404 : 503,
      headers: {
        "Cache-Control": upstream.status === 404 ? "public, max-age=60" : "no-store",
      },
    });
  }

  const contentType = (upstream.headers.get("content-type") ?? "")
    .split(";", 1)[0]
    .toLowerCase();
  if (!IMAGE_TYPES.has(contentType)) {
    await upstream.body?.cancel();
    return new NextResponse(null, {
      status: 502,
      headers: { "Cache-Control": "no-store" },
    });
  }

  const headers = new Headers({
    "Cache-Control": "public, max-age=300, stale-while-revalidate=60",
    "Content-Type": contentType,
    "X-Content-Type-Options": "nosniff",
  });
  const contentLength = upstream.headers.get("content-length");
  if (contentLength) headers.set("Content-Length", contentLength);
  return new NextResponse(request.method === "HEAD" ? null : upstream.body, {
    status: 200,
    headers,
  });
}

export const GET = brandingResponse;
export const HEAD = brandingResponse;
