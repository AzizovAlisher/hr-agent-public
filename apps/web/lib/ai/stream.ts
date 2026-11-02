import {
  normalizeAiMessage,
  normalizeAiSession,
  normalizeHiringMission,
} from "@/lib/ai/normalize";
import type { AiMessageStreamEvent } from "@/lib/ai/types";

function normalizeEvent(name: string, payload: unknown): AiMessageStreamEvent | null {
  const data = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
  const type = (typeof data.type === "string" ? data.type : name).replaceAll("-", "_");

  if (type === "token" || type === "delta" || type === "message_delta" || type === "message.delta") {
    const delta = data.delta ?? data.content ?? data.text;
    return typeof delta === "string"
      ? { type: "message_delta", message_id: data.message_id as string | undefined, delta }
      : null;
  }
  if (type === "session") {
    const session = (data.session ?? data) as Record<string, unknown>;
    if (typeof session.id === "string") {
      try {
        return { type: "session", session: normalizeAiSession(session) };
      } catch {
        return null;
      }
    }
  }
  if (type === "message" || type === "message.completed") {
    const message = (data.message ?? data) as Record<string, unknown>;
    if (typeof message.id === "string" || typeof message.content === "string") {
      try {
        return { type: "message", message: normalizeAiMessage(message) };
      } catch {
        return null;
      }
    }
  }
  if (type === "mission" || type === "mission.updated") {
    const mission = (data.mission ?? data.data ?? data) as Record<string, unknown> | undefined;
    if (!mission || typeof mission.id !== "string") return null;
    try {
      return { type: "mission", mission: normalizeHiringMission(mission) };
    } catch {
      return null;
    }
  }
  if (type === "canvas" || type === "canvas.updated") {
    return {
      type: "canvas",
      name: typeof data.name === "string" ? data.name : "update",
      data: (data.data && typeof data.data === "object" ? data.data : data) as Record<string, unknown>,
    };
  }
  if (type === "done" || type === "complete" || type === "completed") {
    return {
      type: "done",
      session_id: typeof data.session_id === "string" ? data.session_id : undefined,
      mission_id: typeof data.mission_id === "string" ? data.mission_id : undefined,
    };
  }
  if (type === "error") {
    return {
      type: "error",
      message: typeof data.message === "string" ? data.message : "AI request failed.",
    };
  }
  return null;
}

export async function consumeAiEventStream(
  response: Response,
  onEvent: (event: AiMessageStreamEvent) => void,
): Promise<void> {
  const contentType = response.headers.get("content-type") ?? "";
  if (!response.ok) {
    throw new Error(
      response.status === 429
        ? "The AI request limit was reached. Try again shortly."
        : response.status === 403
          ? "You do not have permission to use this AI feature."
          : "The AI request could not be completed.",
    );
  }

  if (!contentType.includes("text/event-stream") || !response.body) {
    const payload = await response.json();
    const event = normalizeEvent("message", payload);
    if (event) onEvent(event);
    const missionIdValue =
      payload && typeof payload === "object"
        ? Reflect.get(payload, "mission_id")
        : undefined;
    const mission =
      payload && typeof payload === "object"
        ? Reflect.get(payload, "mission")
        : undefined;
    const nestedMissionId =
      mission && typeof mission === "object"
        ? Reflect.get(mission, "id")
        : undefined;
    const missionId = typeof missionIdValue === "string"
      ? missionIdValue
      : typeof nestedMissionId === "string"
        ? nestedMissionId
        : undefined;
    onEvent({ type: "done", mission_id: missionId });
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() ?? "";

    for (const block of blocks) {
      let eventName = "message";
      const dataLines: string[] = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (!dataLines.length) continue;
      const raw = dataLines.join("\n");
      let payload: unknown = raw;
      try {
        payload = JSON.parse(raw);
      } catch {
        // Plain text token events are valid SSE too.
      }
      const event = normalizeEvent(eventName, payload);
      if (event) onEvent(event);
    }
    if (done) break;
  }
}
