import "server-only";

const PRIVATE_AI_HEADERS = {
  "Cache-Control": "private, no-store, max-age=0",
  Pragma: "no-cache",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
} as const;

export function privateAiHeaders({
  contentType,
  stream = false,
}: {
  contentType: string;
  stream?: boolean;
}): Headers {
  const headers = new Headers(PRIVATE_AI_HEADERS);
  headers.set(
    "Cache-Control",
    stream
      ? "private, no-store, max-age=0, no-transform"
      : PRIVATE_AI_HEADERS["Cache-Control"],
  );
  headers.set("Content-Type", contentType);
  if (stream) headers.set("X-Accel-Buffering", "no");
  return headers;
}

export function privateAiError(status: number, code: string): Response {
  const message =
    status === 401
      ? "Your session has expired."
      : status === 403
        ? "You do not have permission to use this AI feature."
        : status === 404
          ? "This AI session is no longer available."
          : status === 413
            ? "The AI attachment is too large."
            : status === 429
              ? "The AI request limit was reached. Try again shortly."
              : "The AI request could not be completed.";
  return Response.json(
    { error: { code, message } },
    {
      status,
      headers: privateAiHeaders({ contentType: "application/json; charset=utf-8" }),
    },
  );
}
