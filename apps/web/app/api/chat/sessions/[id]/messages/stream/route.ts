import { getServerAccessToken } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
const MAX_CHAT_MESSAGE_CHARS = 10_000;
const MAX_IDEMPOTENCY_KEY_CHARS = 160;

function jsonError(status: number, code: string, message: string): Response {
  return Response.json(
    { error: { code, message } },
    {
      status,
      headers: {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
      },
    },
  );
}

export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const token = await getServerAccessToken();
  if (!token) {
    return jsonError(401, "chat_unauthorized", "Your session has expired.");
  }

  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return jsonError(400, "chat_invalid_request", "A JSON request is required.");
  }

  const content =
    payload && typeof payload === "object"
      ? Reflect.get(payload, "content")
      : null;
  if (
    typeof content !== "string" ||
    !content.trim() ||
    content.length > MAX_CHAT_MESSAGE_CHARS
  ) {
    return jsonError(
      400,
      "chat_invalid_content",
      `Message content must be between 1 and ${MAX_CHAT_MESSAGE_CHARS} characters.`,
    );
  }

  const suppliedIdempotencyKey = request.headers.get("idempotency-key");
  if (
    suppliedIdempotencyKey !== null &&
    (!suppliedIdempotencyKey ||
      suppliedIdempotencyKey.length > MAX_IDEMPOTENCY_KEY_CHARS)
  ) {
    return jsonError(
      400,
      "chat_invalid_idempotency_key",
      `Idempotency-Key must be between 1 and ${MAX_IDEMPOTENCY_KEY_CHARS} characters.`,
    );
  }
  const idempotencyKey = suppliedIdempotencyKey ?? crypto.randomUUID();

  const { id } = await params;
  let upstream: Response;
  try {
    upstream = await fetch(
      `${API_BASE_URL}/chat/sessions/${encodeURIComponent(id)}/messages/stream`,
      {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({ content }),
        cache: "no-store",
        signal: request.signal,
      },
    );
  } catch (error) {
    if (request.signal.aborted) {
      return jsonError(499, "chat_cancelled", "The chat request was cancelled.");
    }
    console.error("Chat gateway could not reach the API", {
      cause: error instanceof Error ? error.name : "unknown",
    });
    return jsonError(
      502,
      "chat_gateway_unavailable",
      "The assistant is temporarily unavailable.",
    );
  }

  if (!upstream.ok) {
    // Do not proxy upstream bodies: validation/provider errors can contain
    // implementation details. The browser receives a stable typed envelope.
    return jsonError(
      upstream.status,
      `chat_upstream_${upstream.status}`,
      upstream.status === 404
        ? "This conversation is no longer available."
        : "The assistant could not process this message.",
    );
  }

  const contentType = upstream.headers.get("content-type") ?? "";
  if (!upstream.body || !contentType.toLowerCase().includes("text/event-stream")) {
    return jsonError(
      502,
      "chat_invalid_upstream",
      "The assistant returned an invalid response.",
    );
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Cache-Control": "private, no-store, no-transform",
      "Content-Type": "text/event-stream; charset=utf-8",
      "X-Accel-Buffering": "no",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
