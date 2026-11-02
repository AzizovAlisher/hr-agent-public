"use client";

import { useSearchParams } from "next/navigation";
import {
  useEffect,
  useRef,
  useState,
  useTransition,
} from "react";
import {
  Clock,
  CornerDownLeft,
  LayoutGrid,
  Plus,
  RotateCcw,
  User as UserIcon,
} from "lucide-react";

import {
  createChatSessionAction,
  listChatSessionsAction,
  loadChatSessionAction,
} from "@/lib/actions";
import {
  ChatStreamError,
  consumeChatEventStream,
  type ChatUsage,
} from "@/lib/chat-stream";
import { useI18n } from "@/components/i18n/locale-provider";
import type {
  ChatMessage as ApiMessage,
  ChatSessionDetail,
  ChatSessionListItem,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

import { RichText } from "@/components/chat/rich-text";

/** @ui-only Chat workspace scope selector. */
type Scope = "candidate" | "position";

/** @ui-only Chat panel composition properties. */
type ChatProps = {
  scope: Scope;
  candidateId?: string;
  positionId?: string;
  scopeRef?: string;
};

/** @ui-only Optimistic and streamed chat-message view. */
type LocalMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  ts: number;
  pending?: boolean;
  errored?: boolean;
  errorMessage?: string;
  usage?: ChatUsage;
  turnId?: string;
};

/** @ui-only Chat panel view state. */
type View = "active" | "history";

export function ChatPanel({
  scope,
  candidateId,
  positionId,
  scopeRef,
}: ChatProps) {
  const { m } = useI18n();
  const searchParams = useSearchParams();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<LocalMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creatingSession, startCreate] = useTransition();
  const [view, setView] = useState<View>("active");
  const [history, setHistory] = useState<ChatSessionListItem[] | null>(null);
  const [historyLoading, startHistory] = useTransition();
  const [loadingSession, startLoadSession] = useTransition();
  // "Stick to bottom" mirrors how ChatGPT/Claude do it: auto-scroll only when
  // the user is already near the bottom; if they've scrolled up to read older
  // messages, leave them alone.
  const [stickToBottom, setStickToBottom] = useState(true);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const streamAbortRef = useRef<AbortController | null>(null);
  const sendLockRef = useRef(false);
  const initialChatId = searchParams.get("chat");

  const headerLabel =
    scope === "candidate" ? m.chat.headerCandidate : m.chat.headerPosition;
  const placeholder =
    scope === "candidate"
      ? m.chat.placeholderCandidate
      : m.chat.placeholderPosition;
  const emptyTitle =
    scope === "candidate"
      ? m.chat.emptyTitleCandidate
      : m.chat.emptyTitlePosition;
  const emptyDesc =
    scope === "candidate"
      ? m.chat.emptyDescCandidate
      : m.chat.emptyDescPosition;
  const suggestions =
    scope === "candidate"
      ? m.chat.suggestionsCandidate
      : m.chat.suggestionsPosition;

  async function ensureSession(): Promise<string | null> {
    if (sessionId) return sessionId;
    return new Promise((resolve) => {
      startCreate(async () => {
        const res = await createChatSessionAction({
          scope,
          candidate_id: candidateId,
          position_id: positionId,
        });
        if (!res.ok) {
          setError(res.error);
          resolve(null);
          return;
        }
        setSessionId(res.data.id);
        resolve(res.data.id);
      });
    });
  }

  async function sendContent(content: string, retryTurnId?: string) {
    const c = content.trim();
    if (!c || streaming || sendLockRef.current) return;
    sendLockRef.current = true;
    setError(null);
    setStickToBottom(true);

    const sid = await ensureSession();
    if (!sid) {
      sendLockRef.current = false;
      return;
    }

    const turnId = retryTurnId ?? crypto.randomUUID();
    const assistantId = `a-${turnId}`;
    if (retryTurnId) {
      setMessages((current) =>
        current.map((msg) =>
          msg.id === assistantId
            ? {
                ...msg,
                content: "",
                pending: true,
                errored: false,
                errorMessage: undefined,
                usage: undefined,
              }
            : msg,
        ),
      );
    } else {
      const ts = Date.now();
      const userMsg: LocalMessage = {
        id: `u-${turnId}`,
        role: "user",
        content: c,
        ts,
        turnId,
      };
      setMessages((current) => [
        ...current,
        userMsg,
        {
          id: assistantId,
          role: "assistant",
          content: "",
          ts: Date.now(),
          pending: true,
          turnId,
        },
      ]);
    }
    setInput("");
    setStreaming(true);

    const abortController = new AbortController();
    streamAbortRef.current = abortController;

    try {
      const res = await fetch(
        `/api/chat/sessions/${encodeURIComponent(sid)}/messages/stream`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": turnId,
          },
          body: JSON.stringify({ content: c }),
          signal: abortController.signal,
        },
      );

      await consumeChatEventStream(res, (event) => {
        if (event.type === "delta") {
          setMessages((current) =>
            current.map((msg) =>
              msg.id === assistantId
                ? {
                    ...msg,
                    content: msg.content + event.content,
                    pending: false,
                  }
                : msg,
            ),
          );
        } else if (event.type === "usage") {
          setMessages((current) =>
            current.map((msg) =>
              msg.id === assistantId ? { ...msg, usage: event.usage } : msg,
            ),
          );
        } else if (event.type === "done") {
          setMessages((current) =>
            current.map((msg) =>
              msg.id === assistantId ? { ...msg, pending: false } : msg,
            ),
          );
        }
      });
    } catch (e) {
      if (abortController.signal.aborted) return;
      const errMsg =
        e instanceof ChatStreamError
          ? e.code === "chat_unauthorized"
            ? m.login.sessionExpired
            : e.code === "chat_incomplete_stream"
              ? m.chat.streamInterrupted
              : e.message
          : m.chat.streamInterrupted;
      setError(errMsg);
      toast.error(m.chat.failedToast(errMsg));
      // Mark the partial assistant message as errored so the user can tell
      // it was cut off (vs. a complete short reply).
      setMessages((m) =>
        m.map((msg) =>
          msg.id === assistantId
            ? { ...msg, errored: true, errorMessage: errMsg }
            : msg,
        ),
      );
    } finally {
      if (streamAbortRef.current === abortController) {
        streamAbortRef.current = null;
      }
      sendLockRef.current = false;
      setStreaming(false);
      setMessages((m) =>
        m.map((msg) => (msg.id === assistantId ? { ...msg, pending: false } : msg)),
      );
    }
  }

  useEffect(
    () => () => {
      streamAbortRef.current?.abort();
    },
    [],
  );

  function send() {
    void sendContent(input);
  }

  useEffect(() => {
    if (!listRef.current) return;
    if (!stickToBottom) return;
    listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [messages, streaming, stickToBottom]);

  function handleListScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    setStickToBottom(distance < 80);
  }

  // If the URL has ?chat=<id>, load that session on mount.
  useEffect(() => {
    if (!initialChatId || sessionId === initialChatId) return;
    void loadSession(initialChatId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialChatId]);

  function loadHistory() {
    setView("history");
    if (history !== null) return;
    startHistory(async () => {
      const filter: { scope: Scope; position_id?: string; candidate_id?: string } = {
        scope,
      };
      if (positionId) filter.position_id = positionId;
      if (candidateId) filter.candidate_id = candidateId;
      const res = await listChatSessionsAction(filter);
      if (res.ok) {
        setHistory(res.data as ChatSessionListItem[]);
      } else {
        setError(res.error);
        setHistory([]);
      }
    });
  }

  function loadSession(id: string) {
    startLoadSession(async () => {
      const res = await loadChatSessionAction(id);
      if (!res.ok) {
        toast.error(res.error);
        return;
      }
      const detail = res.data as ChatSessionDetail;
      setSessionId(detail.id);
      setMessages(
        detail.messages.map((msg: ApiMessage) => ({
          id: msg.id,
          role: msg.role === "system" ? "assistant" : msg.role,
          content: msg.content,
          ts: new Date(msg.created_at).getTime(),
          usage:
            msg.role === "assistant" && msg.model_used
              ? {
                  modelUsed: msg.model_used,
                  tokensIn: null,
                  tokensOut: null,
                }
              : undefined,
        })),
      );
      setView("active");
      setError(null);
      setStickToBottom(true);
    });
  }

  function newChat() {
    streamAbortRef.current?.abort();
    setSessionId(null);
    setMessages([]);
    setError(null);
    setView("active");
    setStickToBottom(true);
    inputRef.current?.focus();
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background" aria-busy={streaming}>
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-[18px] py-3.5">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accent-muted text-primary">
            {scope === "position" ? (
              <LayoutGrid className="h-3.5 w-3.5" />
            ) : (
              <UserIcon className="h-3.5 w-3.5" />
            )}
          </span>
          <div>
            <div className="text-[12px] font-medium leading-tight">{headerLabel}</div>
            {scopeRef ? (
              <div className="font-mono text-[10px] uppercase tracking-[0.04em] text-text-faint">
                {scopeRef}
              </div>
            ) : null}
          </div>
        </div>
        <div className="flex items-center gap-px">
          <button
            type="button"
            onClick={() => (view === "history" ? setView("active") : loadHistory())}
            title={view === "history" ? m.chat.backToChat : m.chat.history}
            aria-label={view === "history" ? m.chat.backToChat : m.chat.history}
            aria-pressed={view === "history"}
            className={cn(
              "flex h-[26px] w-[26px] items-center justify-center rounded-[6px] transition-colors",
              view === "history"
                ? "bg-bg-active text-text"
                : "text-text-muted hover:bg-bg-hover hover:text-text",
            )}
          >
            <Clock className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={newChat}
            title={m.chat.newChat}
            aria-label={m.chat.newChat}
            className="flex h-[26px] w-[26px] items-center justify-center rounded-[6px] text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* History view */}
      {view === "history" ? (
        <div className="min-h-0 flex-1 overflow-y-auto px-[18px] py-[18px]">
          {historyLoading ? (
            <div className="flex h-full items-center justify-center text-[12px] text-text-faint">
              …
            </div>
          ) : history && history.length > 0 ? (
            <ul className="flex flex-col gap-2">
              {history.map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => loadSession(s.id)}
                    disabled={loadingSession}
                    className={cn(
                      "flex w-full flex-col gap-1 rounded-[6px] border border-border bg-bg-elevated px-3 py-2.5 text-left transition-colors hover:border-border-strong hover:bg-bg-hover disabled:opacity-50",
                      sessionId === s.id && "border-border-strong bg-bg-hover",
                    )}
                  >
                    <p className="line-clamp-1 text-[12.5px] text-text">
                      {s.preview ?? "—"}
                    </p>
                    <div className="flex items-center justify-between font-mono text-[10px] text-text-faint">
                      <span>{m.conversations.messagesCount(s.message_count)}</span>
                      <span>
                        {m.format.relativeTime(s.last_activity ?? s.updated_at)}
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="pt-12 text-center text-[12px] text-text-faint">
              {m.chat.noHistory}
            </p>
          )}
        </div>
      ) : messages.length === 0 ? (
        <div className="flex min-h-0 flex-1 flex-col items-center justify-start gap-3 overflow-y-auto px-6 py-6 text-center min-[420px]:justify-center min-[420px]:py-10">
          <div className="flex h-10 w-10 items-center justify-center rounded-[10px] border border-border bg-bg-elevated text-text-muted">
            {scope === "position" ? (
              <LayoutGrid className="h-4 w-4" />
            ) : (
              <UserIcon className="h-4 w-4" />
            )}
          </div>
          <div className="text-[13px] font-medium">{emptyTitle}</div>
          <p className="max-w-[260px] text-[12px] leading-[1.55] text-text-muted">
            {emptyDesc}
          </p>
          <div className="mt-1.5 flex w-full max-w-[280px] flex-col gap-1.5">
            {suggestions.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => void sendContent(s)}
                disabled={streaming || creatingSession}
                className="rounded-[6px] border border-border bg-bg-elevated px-3 py-2 text-left text-[12px] text-text-muted transition-all hover:border-border-strong hover:bg-bg-hover hover:text-text disabled:opacity-50"
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div
          ref={listRef}
          onScroll={handleListScroll}
          role="log"
          aria-live="polite"
          aria-relevant="additions text"
          aria-label={headerLabel}
          className="min-h-0 flex-1 overflow-y-auto px-[18px] py-[18px]"
        >
          <div className="flex flex-col gap-[18px]">
            {messages.map((msg) => (
              <MessageGroup
                key={msg.id}
                msg={msg}
                retryDisabled={streaming}
                onRetry={
                  msg.role === "assistant" && msg.errored && msg.turnId
                    ? () => {
                        const userTurn = messages.find(
                          (candidate) =>
                            candidate.role === "user" &&
                            candidate.turnId === msg.turnId,
                        );
                        if (userTurn) {
                          void sendContent(userTurn.content, msg.turnId);
                        }
                      }
                    : undefined
                }
              />
            ))}
          </div>
        </div>
      )}

      {/* Input */}
      <div className="shrink-0 border-t border-border px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
        <div className="rounded-lg border border-border bg-bg-elevated transition-colors focus-within:border-border-strong">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={placeholder}
            aria-label={placeholder}
            aria-describedby="chat-composer-status"
            rows={2}
            maxLength={10_000}
            disabled={streaming}
            className="block max-h-[120px] min-h-[38px] w-full resize-none border-none bg-transparent px-3 pb-1.5 pt-2.5 text-[13px] leading-[1.5] outline-none placeholder:text-text-faint"
          />
          <div className="flex items-center justify-between px-2 pb-2 pt-1">
            <span
              id="chat-composer-status"
              className="font-mono text-[10px] text-text-faint"
              aria-live="polite"
            >
              {streaming
                ? m.chat.streaming
                : creatingSession
                  ? m.chat.starting
                  : m.chat.sendHint}
            </span>
            <button
              type="button"
              onClick={send}
              disabled={streaming || creatingSession || !input.trim()}
              className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-2.5 py-1 text-[11px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              <CornerDownLeft className="h-3 w-3" />
              {m.chat.send}
            </button>
          </div>
        </div>
        {error ? (
          <p className="mt-2 text-[11px] text-score-low" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function MessageGroup({
  msg,
  onRetry,
  retryDisabled,
}: {
  msg: LocalMessage;
  onRetry?: () => void;
  retryDisabled: boolean;
}) {
  const { m } = useI18n();
  const time = formatTime(msg.ts);
  const isUser = msg.role === "user";
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <span
          className={cn(
            "text-[11px] font-semibold",
            isUser ? "text-text" : "text-primary",
          )}
        >
          {isUser ? m.chat.you : m.chat.assistant}
        </span>
        <span className="font-mono text-[10px] text-text-faint">{time}</span>
      </div>
      {isUser ? (
        <div className="whitespace-pre-wrap text-[13px] leading-[1.6] text-text-muted">
          {msg.content}
        </div>
      ) : msg.content ? (
        <div className="text-text">
          <RichText text={msg.content} />
          {msg.usage ? (
            <p className="mt-1.5 font-mono text-[10px] text-text-faint">
              {m.chat.usage(
                msg.usage.modelUsed,
                (msg.usage.tokensIn ?? 0) + (msg.usage.tokensOut ?? 0),
              )}
            </p>
          ) : null}
          {msg.errored ? (
            <RetryableError
              message={msg.errorMessage ?? m.chat.streamInterrupted}
              onRetry={onRetry}
              disabled={retryDisabled}
            />
          ) : null}
        </div>
      ) : msg.pending ? (
        <div className="text-[13px] leading-[1.6] text-text">
          <TypingDots />
        </div>
      ) : msg.errored ? (
        <RetryableError
          message={msg.errorMessage ?? m.chat.streamInterrupted}
          onRetry={onRetry}
          disabled={retryDisabled}
        />
      ) : null}
    </div>
  );
}

function RetryableError({
  message,
  onRetry,
  disabled,
}: {
  message: string;
  onRetry?: () => void;
  disabled: boolean;
}) {
  const { m } = useI18n();
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md border border-score-low/30 bg-score-low/10 px-2 py-1.5 text-[11px] text-score-low">
      <span role="alert">{message}</span>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={disabled}
          className="inline-flex min-h-7 items-center gap-1 rounded border border-score-low/40 px-2 font-medium transition-colors hover:bg-score-low/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-score-low disabled:opacity-50"
        >
          <RotateCcw className="h-3 w-3" aria-hidden="true" />
          {m.chat.retry}
        </button>
      ) : null}
    </div>
  );
}

function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1 py-1">
      <span className="typing-dot h-1.5 w-1.5 rounded-full bg-text-muted" />
      <span
        className="typing-dot h-1.5 w-1.5 rounded-full bg-text-muted"
        style={{ animationDelay: "0.2s" }}
      />
      <span
        className="typing-dot h-1.5 w-1.5 rounded-full bg-text-muted"
        style={{ animationDelay: "0.4s" }}
      />
    </span>
  );
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  return `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}
