import { NextResponse, type NextRequest } from "next/server";

export async function GET(
  request: NextRequest,
  context: RouteContext<"/assessment/language/[lang]">,
): Promise<Response> {
  const { lang: requested } = await context.params;
  const lang = requested === "ru" ? "ru" : "en";
  const destination = new URL("/assessment", request.url);
  destination.searchParams.set("lang", lang);
  const status = request.nextUrl.searchParams.get("status");
  if (status === "invalid" || status === "expired" || status === "unavailable") {
    destination.searchParams.set("status", status);
  }
  const response = NextResponse.redirect(destination, 303);
  response.cookies.set("locale", lang, {
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
    sameSite: "lax",
  });
  response.headers.set("Cache-Control", "no-store, max-age=0");
  response.headers.set("Referrer-Policy", "no-referrer");
  return response;
}
