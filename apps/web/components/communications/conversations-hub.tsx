"use client";

import Link from "next/link";
import { useMemo, useRef, useState, useTransition } from "react";
import {
  ArrowUp,
  Bot,
  CheckCheck,
  Loader2,
  Mail,
  MessageCircle,
  RefreshCw,
  Send,
} from "lucide-react";
import { toast } from "sonner";

import { ConversationsList } from "@/components/chat/conversations-list";
import { useI18n } from "@/components/i18n/locale-provider";
import { SourceBadge } from "@/components/ui/source-badge";
import {
  listCommunicationMessagesAction,
  listCommunicationThreadsAction,
  markCommunicationThreadReadAction,
} from "@/lib/actions";
import { candDisplayRef } from "@/lib/format";
import type {
  ChatSessionListItem,
  CommunicationMessage,
  CommunicationThread,
  CommunicationThreadPage,
  MessageChannel,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/** @ui-only Conversations-hub tab state. */
type HubTab = "inbox" | "ai";
/** @ui-only Conversations-hub channel filter. */
type ChannelFilter = "all" | MessageChannel;

function uniqueById<T extends { id: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

function messageTime(message: CommunicationMessage): string {
  return (
    message.received_at ??
    message.delivered_at ??
    message.sent_at ??
    message.scheduled_for ??
    message.created_at
  );
}

export function ConversationsHub({
  sessions,
  chatLoadFailed,
  initialInbox,
  inboxLoadFailed,
  access,
}: {
  sessions: ChatSessionListItem[];
  chatLoadFailed: boolean;
  initialInbox: CommunicationThreadPage;
  inboxLoadFailed: boolean;
  access: {
    canUseAi: boolean;
    canReadInbox: boolean;
    canManageInbox: boolean;
  };
}) {
  const { m } = useI18n();
  const [tab, setTab] = useState<HubTab>(
    access.canReadInbox ? "inbox" : "ai",
  );
  const [threads, setThreads] = useState(initialInbox.items);
  const [nextCursor, setNextCursor] = useState(initialInbox.next_cursor);
  const [channel, setChannel] = useState<ChannelFilter>("all");
  const [threadsFailed, setThreadsFailed] = useState(inboxLoadFailed);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<CommunicationMessage[]>([]);
  const [messageCursor, setMessageCursor] = useState<string | null>(null);
  const [messagesFailed, setMessagesFailed] = useState(false);
  const [threadsPending, startThreads] = useTransition();
  const [messagesPending, startMessages] = useTransition();
  const threadGeneration = useRef(0);
  const messageGeneration = useRef(0);

  const unreadCount = useMemo(
    () => threads.reduce((total, thread) => total + thread.unread_count, 0),
    [threads],
  );
  const selectedThread = threads.find((thread) => thread.id === selectedThreadId);

  function loadThreads(
    nextChannel: ChannelFilter,
    cursor: string | null = null,
    append = false,
  ) {
    if (!access.canReadInbox) return;
    const generation = ++threadGeneration.current;
    if (!append) {
      messageGeneration.current += 1;
      setChannel(nextChannel);
      setSelectedThreadId(null);
      setMessages([]);
      setMessageCursor(null);
      setMessagesFailed(false);
      setThreads([]);
      setNextCursor(null);
    }
    setThreadsFailed(false);
    startThreads(async () => {
      const result = await listCommunicationThreadsAction({
        channel: nextChannel === "all" ? null : nextChannel,
        cursor,
      });
      if (generation !== threadGeneration.current) return;
      if (!result.ok) {
        setThreadsFailed(true);
        return;
      }
      setThreads((current) =>
        append
          ? uniqueById([...current, ...result.data.items])
          : result.data.items,
      );
      setNextCursor(result.data.next_cursor);
    });
  }

  function loadMessages(threadId: string, cursor: string | null, append: boolean) {
    if (!access.canReadInbox) return;
    const generation = ++messageGeneration.current;
    setMessagesFailed(false);
    startMessages(async () => {
      const result = await listCommunicationMessagesAction(threadId, cursor);
      if (
        generation !== messageGeneration.current ||
        selectedThreadId !== threadId
      ) {
        return;
      }
      if (!result.ok) {
        setMessagesFailed(true);
        return;
      }
      setMessages((current) =>
        append
          ? uniqueById([...current, ...result.data.items])
          : result.data.items,
      );
      setMessageCursor(result.data.next_cursor);
    });
  }

  function selectThread(thread: CommunicationThread) {
    if (!access.canReadInbox) return;
    setSelectedThreadId(thread.id);
    setMessages([]);
    setMessageCursor(null);
    setMessagesFailed(false);
    const generation = ++messageGeneration.current;
    startMessages(async () => {
      const [messageResult, readResult] = await Promise.all([
        listCommunicationMessagesAction(thread.id),
        access.canManageInbox && thread.unread_count > 0
          ? markCommunicationThreadReadAction(thread.id)
          : Promise.resolve(null),
      ]);
      if (generation !== messageGeneration.current) return;
      if (readResult && !readResult.ok) {
        toast.error(m.communications.markReadFailed);
      } else if (readResult?.ok) {
        setThreads((current) =>
          current.map((item) =>
            item.id === thread.id
              ? { ...item, unread_count: readResult.data.unread_count }
              : item,
          ),
        );
      }
      if (!messageResult.ok) {
        setMessagesFailed(true);
        return;
      }
      setMessages(messageResult.data.items);
      setMessageCursor(messageResult.data.next_cursor);
    });
  }

  const tabs: { id: HubTab; label: string; count: number; icon: typeof Mail }[] = [
    ...(access.canReadInbox
      ? [{
          id: "inbox" as const,
          label: m.communications.inboxTab,
          count: unreadCount,
          icon: Mail,
        }]
      : []),
    ...(access.canUseAi
      ? [{
          id: "ai" as const,
          label: m.communications.aiTab,
          count: sessions.length,
          icon: Bot,
        }]
      : []),
  ];

  if (tabs.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border px-5 py-14 text-center text-sm text-text-muted">
        {m.communications.accessDenied}
      </div>
    );
  }

  return (
    <>
      <div className="mb-5 flex items-center gap-1 border-b border-border">
        {tabs.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              aria-pressed={tab === item.id}
              onClick={() => setTab(item.id)}
              className={cn(
                "-mb-px inline-flex items-center gap-2 border-b px-3.5 py-3 text-[12px] font-medium transition-colors",
                tab === item.id
                  ? "border-text text-text"
                  : "border-transparent text-text-muted hover:text-text",
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {item.label}
              {item.count > 0 ? (
                <span className="rounded-full bg-bg-active px-1.5 py-0.5 font-mono text-[9.5px] text-text-muted">
                  {item.count > 99 ? "99+" : item.count}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      {tab === "ai" ? (
        chatLoadFailed ? (
          <LoadFailure
            message={m.communications.aiLoadFailed}
            retry={() => window.location.reload()}
          />
        ) : (
          <ConversationsList sessions={sessions} />
        )
      ) : (
        <div>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-[14px] font-semibold text-text">
                {m.communications.inboxTitle}
              </h2>
              <p className="mt-0.5 text-[11px] text-text-faint">
                {m.communications.inboxSummary(threads.length)}
              </p>
            </div>
            <div
              className="flex flex-wrap gap-1"
              role="group"
              aria-label={m.communications.channelFilter}
            >
              {(["all", "email", "telegram"] as const).map(
                (value) => (
                  <button
                    key={value}
                    type="button"
                    disabled={threadsPending}
                    aria-pressed={channel === value}
                    onClick={() => loadThreads(value)}
                    className={cn(
                      "rounded-[6px] border px-2.5 py-1.5 text-[10.5px] transition-colors disabled:opacity-50",
                      channel === value
                        ? "border-border-strong bg-bg-active text-text"
                        : "border-border bg-bg-elevated text-text-muted hover:bg-bg-hover",
                    )}
                  >
                    {value === "all"
                      ? m.communications.allChannels
                      : value === "email"
                        ? "Email"
                        : "Telegram"}
                  </button>
                ),
              )}
            </div>
          </div>

          {threadsPending && threads.length === 0 ? (
            <LoadingState label={m.communications.loadingThreads} />
          ) : threadsFailed && threads.length === 0 ? (
            <LoadFailure
              message={m.communications.loadFailed}
              retry={() => loadThreads(channel)}
            />
          ) : threads.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-5 py-14 text-center text-[12px] text-text-muted">
              {m.communications.inboxEmpty}
            </div>
          ) : (
            <div className="grid min-h-[480px] overflow-hidden rounded-xl border border-border bg-bg-elevated lg:grid-cols-[330px_1fr]">
              <div className="border-b border-border lg:border-b-0 lg:border-r">
                <ul className="divide-y divide-border">
                  {threads.map((thread) => (
                    <ThreadRow
                      key={thread.id}
                      thread={thread}
                      active={selectedThreadId === thread.id}
                      onSelect={() => selectThread(thread)}
                    />
                  ))}
                </ul>
                {nextCursor ? (
                  <div className="border-t border-border p-3 text-center">
                    <button
                      type="button"
                      disabled={threadsPending}
                      onClick={() => loadThreads(channel, nextCursor, true)}
                      className="inline-flex items-center gap-1.5 rounded-[6px] px-3 py-1.5 text-[11px] text-text-muted hover:bg-bg-hover disabled:opacity-50"
                    >
                      {threadsPending ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : null}
                      {m.communications.loadMoreThreads}
                    </button>
                  </div>
                ) : null}
                {threadsFailed && threads.length > 0 ? (
                  <p className="border-t border-border px-3 py-2 text-center text-[10.5px] text-score-low">
                    {m.communications.loadMoreFailed}
                  </p>
                ) : null}
              </div>

              <div className="min-w-0 bg-bg">
                {!selectedThread ? (
                  <div className="flex min-h-[480px] items-center justify-center px-8 text-center text-[12px] text-text-faint">
                    {m.communications.selectThread}
                  </div>
                ) : (
                  <MessagePane
                    thread={selectedThread}
                    messages={messages}
                    nextCursor={messageCursor}
                    pending={messagesPending}
                    failed={messagesFailed}
                    onRetry={() =>
                      loadMessages(selectedThread.id, null, false)
                    }
                    onLoadMore={() =>
                      loadMessages(selectedThread.id, messageCursor, true)
                    }
                  />
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );
}

function ThreadRow({
  thread,
  active,
  onSelect,
}: {
  thread: CommunicationThread;
  active: boolean;
  onSelect: () => void;
}) {
  const { m } = useI18n();
  const label = candDisplayRef(thread.candidate_id, thread.candidate_source_ref);
  return (
    <li className={cn("flex items-stretch", active && "bg-bg-active")}>
      <button
        type="button"
        onClick={onSelect}
        className="min-w-0 flex-1 px-4 py-3 text-left transition-colors hover:bg-bg-hover"
      >
        <span className="flex items-center gap-2">
          {thread.candidate_source ? (
            <SourceBadge source={thread.candidate_source} />
          ) : null}
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] font-medium text-text">
            {label}
          </span>
          {thread.unread_count > 0 ? (
            <span className="rounded-full bg-primary px-1.5 py-0.5 font-mono text-[9px] text-white">
              {thread.unread_count > 99 ? "99+" : thread.unread_count}
            </span>
          ) : null}
        </span>
        <span className="mt-1 flex items-center gap-1.5 text-[9.5px] uppercase tracking-[0.06em] text-text-faint">
          {thread.channel === "email" ? (
            <Mail className="h-3 w-3" />
          ) : (
            <MessageCircle className="h-3 w-3" />
          )}
          {thread.channel}
          {thread.latest_message?.direction === "inbound" ? (
            <span className="inline-flex items-center gap-1 text-primary">
              · {m.communications.inbound}
            </span>
          ) : null}
        </span>
        <span
          className={cn(
            "mt-1 block truncate text-[11.5px]",
            thread.unread_count > 0
              ? "font-medium text-text"
              : "text-text-muted",
          )}
        >
          {thread.latest_message?.body ?? m.communications.noMessages}
        </span>
        <span className="mt-1 block font-mono text-[9.5px] text-text-faint">
          {m.format.relativeTime(thread.last_message_at ?? thread.created_at)}
        </span>
      </button>
      <Link
        href={`/candidates/${thread.candidate_id}`}
        title={m.communications.openCandidate}
        aria-label={m.communications.openCandidate}
        className="flex w-9 shrink-0 items-center justify-center border-l border-border text-text-faint hover:bg-bg-hover hover:text-text"
      >
        <ArrowUp className="h-3.5 w-3.5 rotate-45" />
      </Link>
    </li>
  );
}

function MessagePane({
  thread,
  messages,
  nextCursor,
  pending,
  failed,
  onRetry,
  onLoadMore,
}: {
  thread: CommunicationThread;
  messages: CommunicationMessage[];
  nextCursor: string | null;
  pending: boolean;
  failed: boolean;
  onRetry: () => void;
  onLoadMore: () => void;
}) {
  const { locale, m } = useI18n();
  const ordered = [...messages].reverse();
  const candidateLabel = candDisplayRef(
    thread.candidate_id,
    thread.candidate_source_ref,
  );
  const dateLocale = locale === "ru" ? "ru-RU" : "en-US";
  return (
    <div className="flex min-h-[480px] flex-col">
      <div className="flex items-center justify-between gap-3 border-b border-border bg-bg-elevated px-4 py-3">
        <div className="min-w-0">
          <Link
            href={`/candidates/${thread.candidate_id}`}
            className="truncate font-mono text-[11.5px] font-medium text-text hover:underline"
          >
            {candidateLabel}
          </Link>
          <p className="mt-0.5 text-[9.5px] uppercase tracking-[0.06em] text-text-faint">
            {thread.channel}
          </p>
        </div>
        {thread.unread_count === 0 ? (
          <span className="inline-flex items-center gap-1 text-[10px] text-text-faint">
            <CheckCheck className="h-3.5 w-3.5" />
            {m.communications.read}
          </span>
        ) : null}
      </div>

      <div className="flex-1 space-y-2 overflow-y-auto p-4">
        {pending && messages.length === 0 ? (
          <LoadingState label={m.communications.loadingMessages} />
        ) : failed && messages.length === 0 ? (
          <LoadFailure message={m.communications.loadMessagesFailed} retry={onRetry} />
        ) : messages.length === 0 ? (
          <p className="py-12 text-center text-[11px] text-text-faint">
            {m.communications.noMessages}
          </p>
        ) : (
          <>
            {nextCursor ? (
              <div className="pb-2 text-center">
                <button
                  type="button"
                  disabled={pending}
                  onClick={onLoadMore}
                  className="inline-flex items-center gap-1.5 rounded-[6px] px-3 py-1.5 text-[10.5px] text-text-muted hover:bg-bg-hover disabled:opacity-50"
                >
                  {pending ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : null}
                  {m.communications.loadMore}
                </button>
              </div>
            ) : null}
            {failed && messages.length > 0 ? (
              <p className="pb-2 text-center text-[10.5px] text-score-low">
                {m.communications.loadMoreFailed}
              </p>
            ) : null}
            {ordered.map((message) => {
              const timestamp = messageTime(message);
              const absoluteTime = new Intl.DateTimeFormat(dateLocale, {
                dateStyle: "medium",
                timeStyle: "short",
              }).format(new Date(timestamp));
              return (
                <article
                  key={message.id}
                  className={cn(
                    "max-w-[88%] rounded-lg border px-3 py-2.5",
                    message.direction === "outbound"
                      ? "ml-auto border-primary/20 bg-primary/5"
                      : "border-border bg-bg-elevated",
                  )}
                >
                  <div className="mb-1 flex items-center justify-between gap-3 text-[9.5px] text-text-faint">
                    <span className="inline-flex items-center gap-1">
                      {message.direction === "outbound" ? (
                        <Send className="h-3 w-3" />
                      ) : (
                        <MessageCircle className="h-3 w-3" />
                      )}
                      {message.direction === "outbound"
                        ? m.communications.outbound
                        : m.communications.inbound}
                    </span>
                    <time dateTime={timestamp} title={absoluteTime}>
                      {m.format.relativeTime(timestamp)}
                    </time>
                  </div>
                  {message.subject ? (
                    <p className="mb-1 text-[11px] font-medium text-text">
                      {message.subject}
                    </p>
                  ) : null}
                  <p className="whitespace-pre-wrap break-words text-[11.5px] leading-5 text-text-muted">
                    {message.body}
                  </p>
                  <p
                    className={cn(
                      "mt-1.5 text-right text-[9px]",
                      message.status === "failed"
                        ? "text-score-low"
                        : "text-text-faint",
                    )}
                  >
                    {m.communications.statuses[message.status] ?? message.status}
                    {message.failure_code
                      ? ` · ${m.communications.failureCodes[message.failure_code] ?? message.failure_code}`
                      : ""}
                  </p>
                </article>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}

function LoadingState({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center gap-2 px-5 py-12 text-[12px] text-text-muted">
      <Loader2 className="h-4 w-4 animate-spin" /> {label}
    </div>
  );
}

function LoadFailure({
  message,
  retry,
}: {
  message: string;
  retry: () => void;
}) {
  const { m } = useI18n();
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-score-low/25 bg-score-low/5 px-5 py-12 text-center">
      <p className="text-[12px] text-score-low">{message}</p>
      <button
        type="button"
        onClick={retry}
        className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-3 py-1.5 text-[11px] text-text-muted hover:bg-bg-hover"
      >
        <RefreshCw className="h-3.5 w-3.5" /> {m.communications.retry}
      </button>
    </div>
  );
}
