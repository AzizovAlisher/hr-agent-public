/** @ui-only Normalized usage view emitted by the SSE parser below. */
export type ChatUsage = {
  modelUsed: string | null;
  tokensIn: number | null;
  tokensOut: number | null;
};

/** @ui-only Normalized discriminated event emitted by the SSE parser below. */
export type ChatStreamEvent =
  | { type: "delta"; content: string }
  | { type: "usage"; usage: ChatUsage }
  | { type: "done"; messageId: string | null }
  | { type: "error"; code: string; message: string };

export class ChatStreamError extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "ChatStreamError";
    this.code = code;
  }
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function optionalCount(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function parseBlock(block: string): { name: string; payload: unknown } | null {
  let name = "message";
  const data: string[] = [];

  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      name = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      data.push(line.slice(5).trimStart());
    }
  }

  if (!data.length) return null;
  const raw = data.join("\n");
  if (raw === "[DONE]") return { name: "done", payload: {} };

  try {
    return { name, payload: JSON.parse(raw) as unknown };
  } catch {
    // Legacy delta-only streams used plain text data. Keep that compatibility
    // without treating arbitrary plain text as a terminal or error event.
    return name === "delta" || name === "message"
      ? { name: "delta", payload: { content: raw.replaceAll("\\n", "\n") } }
      : null;
  }
}

function normalizedEvents(name: string, payload: unknown): ChatStreamEvent[] {
  const data =
    payload && typeof payload === "object"
      ? (payload as Record<string, unknown>)
      : {};
  const type = optionalString(data.type) ?? name;

  if (type === "delta" || type === "token" || type === "message_delta") {
    const content = data.content ?? data.delta ?? data.text;
    return typeof content === "string" && content
      ? [{ type: "delta", content }]
      : [];
  }

  if (type === "usage") {
    return [
      {
        type: "usage",
        usage: {
          modelUsed: optionalString(data.model_used ?? data.model),
          tokensIn: optionalCount(data.tokens_in ?? data.input_tokens),
          tokensOut: optionalCount(data.tokens_out ?? data.output_tokens),
        },
      },
    ];
  }

  if (type === "done" || type === "complete" || type === "completed") {
    const events: ChatStreamEvent[] = [];
    const modelUsed = optionalString(data.model_used ?? data.model);
    const tokensIn = optionalCount(data.tokens_in ?? data.input_tokens);
    const tokensOut = optionalCount(data.tokens_out ?? data.output_tokens);
    if (modelUsed !== null || tokensIn !== null || tokensOut !== null) {
      events.push({
        type: "usage",
        usage: { modelUsed, tokensIn, tokensOut },
      });
    }
    events.push({
      type: "done",
      messageId: optionalString(data.message_id ?? data.id),
    });
    return events;
  }

  if (type === "error") {
    return [
      {
        type: "error",
        code: optionalString(data.code) ?? "chat_stream_error",
        message:
          optionalString(data.message) ??
          "The assistant is temporarily unavailable.",
      },
    ];
  }

  return [];
}

async function responseError(response: Response): Promise<ChatStreamError> {
  let code = `chat_http_${response.status}`;
  let message = `Chat request failed (${response.status}).`;
  try {
    const payload = await response.json();
    const nestedError =
      payload && typeof payload === "object"
        ? Reflect.get(payload, "error")
        : undefined;
    const error =
      nestedError && typeof nestedError === "object" ? nestedError : payload;
    code =
      optionalString(
        error && typeof error === "object" ? Reflect.get(error, "code") : undefined,
      ) ?? code;
    message =
      optionalString(
        error && typeof error === "object"
          ? Reflect.get(error, "message")
          : undefined,
      ) ?? message;
  } catch {
    // The same-origin gateway intentionally does not expose arbitrary upstream
    // response text. Retain the stable, user-safe status message.
  }
  return new ChatStreamError(code, message);
}

/**
 * Consume the typed chat SSE contract. A successful request is only complete
 * after a terminal `done` event; an EOF without it is treated as interruption.
 */
export async function consumeChatEventStream(
  response: Response,
  onEvent: (event: ChatStreamEvent) => void,
): Promise<void> {
  if (!response.ok) throw await responseError(response);

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("text/event-stream") || !response.body) {
    throw new ChatStreamError(
      "chat_invalid_stream",
      "The assistant returned an invalid response.",
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let complete = false;

  const consumeBlock = (block: string) => {
    const parsed = parseBlock(block);
    if (!parsed) return;
    for (const event of normalizedEvents(parsed.name, parsed.payload)) {
      onEvent(event);
      if (event.type === "done") complete = true;
      if (event.type === "error") {
        throw new ChatStreamError(event.code, event.message);
      }
    }
  };

  try {
    while (!complete) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? "";
      for (const block of blocks) consumeBlock(block);
      if (done) {
        if (buffer.trim()) consumeBlock(buffer);
        break;
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }

  if (!complete) {
    throw new ChatStreamError(
      "chat_incomplete_stream",
      "The assistant response ended before it was complete.",
    );
  }
}
