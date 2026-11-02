"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  Briefcase,
  Check,
  ChevronDown,
  CornerDownLeft,
  DollarSign,
  FileText,
  Files,
  Folder,
  Globe,
  MapPin,
  Paperclip,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
  Upload,
  Users,
  X,
} from "lucide-react";

import {
  createPositionFromDraftAction,
  draftPositionAction,
  extractFileTextAction,
  type DraftMessage,
  type PositionDraft,
  type QuickReply,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";
import { RichText } from "@/components/chat/rich-text";
import { StatusBadge } from "@/components/ui/status-badge";
import { toast } from "sonner";

const EMPTY_DRAFT: PositionDraft = {
  title: null,
  description: null,
  requirements: null,
  seniority: null,
  work_format: null,
  location: null,
  employment_type: null,
  salary_range: null,
  team_size: null,
  folder: null,
};

/** @ui-only Optimistic position-draft message view. */
type LocalMsg = DraftMessage & {
  id: string;
  ts: number;
  attachment?: { filename: string; chars: number };
};

/**
 * Position drafts can contain confidential requisition text, compensation, and
 * attachment names. Scope every browser key to the authenticated organization
 * and user so a shared browser never restores one tenant's draft for another.
 * Unscoped legacy keys are deliberately discarded instead of migrated because
 * their owner cannot be established safely.
 */
const DRAFTS_KEY_PREFIX = "hr.position-drafts.v3";
const UNSCOPED_DRAFT_KEYS = [
  "hr.position-drafts.v2",
  "hr.new-position-draft.v1",
] as const;

/** @ui-only Position-draft slot request state. */
type DraftSlotState = {
  messages: LocalMsg[];
  draft: PositionDraft;
  isComplete: boolean;
  missing: string[];
  quickReplies: QuickReply[];
};

/** @ui-only Position-draft slot presentation state. */
type DraftSlot = DraftSlotState & {
  id: string;
  name: string;
  updatedAt: number;
};

/** @ui-only Persisted local position-draft workspace state. */
type DraftStore = {
  active: string | null;
  drafts: Record<string, DraftSlot>;
};

function emptyStore(): DraftStore {
  return { active: null, drafts: {} };
}

function newSlotId(): string {
  return `d-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

function draftStoreKey(draftScope: string): string {
  return `${DRAFTS_KEY_PREFIX}:${encodeURIComponent(draftScope)}`;
}

function loadDraftStore(draftScope: string): DraftStore {
  if (typeof window === "undefined") return emptyStore();
  try {
    for (const key of UNSCOPED_DRAFT_KEYS) {
      window.localStorage.removeItem(key);
    }
    const raw = window.localStorage.getItem(draftStoreKey(draftScope));
    if (raw) {
      const parsed = JSON.parse(raw) as DraftStore;
      if (parsed && typeof parsed === "object" && parsed.drafts) {
        return {
          active: parsed.active ?? null,
          drafts: parsed.drafts ?? {},
        };
      }
    }
  } catch {
    /* fall through to empty */
  }
  return emptyStore();
}

function persistDraftStore(draftScope: string, store: DraftStore) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(draftStoreKey(draftScope), JSON.stringify(store));
  } catch {
    /* quota or private mode */
  }
}

function deriveDraftName(state: DraftSlotState): string {
  if (state.draft.title && state.draft.title.trim()) {
    return state.draft.title.trim().slice(0, 80);
  }
  const firstUser = state.messages.find((m) => m.role === "user");
  if (firstUser) {
    const snippet = (firstUser.attachment?.filename ?? firstUser.content)
      .replace(/\s+/g, " ")
      .trim();
    if (snippet) return snippet.slice(0, 60);
  }
  return "";
}

function isEmptySlot(state: DraftSlotState): boolean {
  return (
    state.messages.length <= 1 &&
    !state.draft.title &&
    !state.draft.description &&
    (!state.draft.requirements ||
      Object.keys(state.draft.requirements).length === 0)
  );
}

const ACCEPT_FILES = ".pdf,.docx,.txt,.md";

function formatChars(n: number): string {
  if (n < 1000) return `${n} chars`;
  return `${(n / 1000).toFixed(1)}k chars`;
}

export function NewPositionFlow({
  availableFolders = [],
  draftScope,
}: {
  availableFolders?: string[];
  draftScope: string;
}) {
  const { m } = useI18n();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  function makeGreeting(): LocalMsg {
    return {
      id: "greet",
      role: "assistant",
      content: m.newPosition.initialGreeting,
      ts: Date.now(),
    };
  }

  const [messages, setMessages] = useState<LocalMsg[]>([makeGreeting()]);

  const REQUIRED_LABEL: Record<string, string> = {
    title: m.newPosition.fieldTitle,
    description: m.newPosition.fieldDescription,
    requirements: m.newPosition.fieldRequirements,
  };
  const openLabel = (m.status as Record<string, string>)["open"] ?? "open";
  const [draft, setDraft] = useState<PositionDraft>(EMPTY_DRAFT);
  const [isComplete, setIsComplete] = useState(false);
  const [missing, setMissing] = useState<string[]>([
    "title",
    "description",
    "requirements",
  ]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [quickReplies, setQuickReplies] = useState<QuickReply[]>([]);
  const [restored, setRestored] = useState(false);
  // Multi-draft state
  const [store, setStore] = useState<DraftStore>(emptyStore());
  const [activeId, setActiveId] = useState<string | null>(null);
  // Auto-scroll only when the user is already near the bottom (Claude-style).
  const [stickToBottom, setStickToBottom] = useState(true);

  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function handleListScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    setStickToBottom(distance < 80);
  }

  // Apply a slot's saved state to the live editing surface.
  function applySlot(slot: DraftSlotState | null) {
    if (slot && slot.messages.length > 0) {
      setMessages(slot.messages);
      setDraft(slot.draft);
      setIsComplete(slot.isComplete);
      setMissing(slot.missing);
      setQuickReplies(slot.quickReplies ?? []);
    } else {
      setMessages([makeGreeting()]);
      setDraft(EMPTY_DRAFT);
      setIsComplete(false);
      setMissing(["title", "description", "requirements"]);
      setQuickReplies([]);
    }
    setError(null);
    setInput("");
    setStickToBottom(true);
  }

  // Restore the store on mount. Pick active draft from URL ?draft=, else
  // fall back to whatever was last active in storage.
  useEffect(() => {
    const loaded = loadDraftStore(draftScope);
    setStore(loaded);
    const fromUrl = searchParams.get("draft");
    let id: string | null = null;
    if (fromUrl && loaded.drafts[fromUrl]) id = fromUrl;
    else if (loaded.active && loaded.drafts[loaded.active]) id = loaded.active;
    setActiveId(id);
    if (id) applySlot(loaded.drafts[id]);
    setRestored(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep ?draft=<id> in the URL in sync with the active id (replace, no scroll).
  useEffect(() => {
    if (!restored) return;
    const current = searchParams.get("draft");
    if (activeId && current !== activeId) {
      router.replace(`${pathname}?draft=${activeId}`, { scroll: false });
    } else if (!activeId && current) {
      router.replace(pathname, { scroll: false });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [restored, activeId]);

  // Persist the active slot whenever its content changes. Empty slots get
  // removed so the switcher doesn't show stale untouched entries.
  useEffect(() => {
    if (!restored) return;
    const slotState: DraftSlotState = {
      messages,
      draft,
      isComplete,
      missing,
      quickReplies,
    };
    const empty = isEmptySlot(slotState);

    setStore((prev) => {
      const next: DraftStore = { ...prev, drafts: { ...prev.drafts } };

      if (empty && !activeId) return prev; // nothing to do

      // Auto-create a slot the first time the user produces content.
      let id = activeId;
      if (!id && !empty) {
        id = newSlotId();
        setActiveId(id);
      }
      if (!id) return prev;

      if (empty) {
        // Editing an existing slot back down to empty: drop it from the store.
        delete next.drafts[id];
        next.active = null;
        setActiveId(null);
      } else {
        next.drafts[id] = {
          ...slotState,
          id,
          name: deriveDraftName(slotState),
          updatedAt: Date.now(),
        };
        next.active = id;
      }
      persistDraftStore(draftScope, next);
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [restored, messages, draft, isComplete, missing, quickReplies]);

  function switchToDraft(id: string) {
    if (id === activeId) return;
    const slot = store.drafts[id];
    if (!slot) return;
    setActiveId(id);
    applySlot(slot);
  }

  function startNewDraft() {
    setActiveId(null);
    applySlot(null);
    inputRef.current?.focus();
  }

  function deleteDraft(id: string) {
    setStore((prev) => {
      const next: DraftStore = { ...prev, drafts: { ...prev.drafts } };
      delete next.drafts[id];
      if (next.active === id) next.active = null;
      persistDraftStore(draftScope, next);
      return next;
    });
    if (activeId === id) {
      setActiveId(null);
      applySlot(null);
    }
  }

  const draftList = Object.values(store.drafts).sort(
    (a, b) => b.updatedAt - a.updatedAt,
  );

  useEffect(() => {
    if (!listRef.current) return;
    if (!stickToBottom) return;
    listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [messages, pending, quickReplies, stickToBottom]);

  async function sendContent(content: string) {
    const c = content.trim();
    if (!c || pending || committing) return;
    setError(null);
    setQuickReplies([]);
    setStickToBottom(true);

    const userMsg: LocalMsg = {
      id: `u-${Date.now()}`,
      role: "user",
      content: c,
      ts: Date.now(),
    };
    const next = [...messages, userMsg];
    setMessages(next);
    setInput("");
    setPending(true);

    const wireMessages: DraftMessage[] = next.map(({ role, content }) => ({
      role,
      content,
    }));

    const res = await draftPositionAction(wireMessages, draft);
    setPending(false);

    if (!res.ok) {
      setError(res.error);
      toast.error(res.error);
      return;
    }

    setMessages((prev) => [
      ...prev,
      {
        id: `a-${Date.now()}`,
        role: "assistant",
        content: res.data.assistant_message,
        ts: Date.now(),
      },
    ]);
    setDraft(res.data.draft);
    setIsComplete(res.data.is_complete);
    setMissing(res.data.missing_required);
    setQuickReplies(res.data.quick_replies ?? []);
  }

  async function send() {
    await sendContent(input);
  }

  function pickQuickReply(qr: QuickReply) {
    if (pending || committing) return;
    // Optimistically patch the structured slot, then echo the choice as the
    // user's reply so the model knows we answered and can ask the next thing.
    patchDraft({ [qr.field]: qr.value } as Partial<PositionDraft>);
    void sendContent(qr.label);
  }

  async function handleFile(file: File) {
    if (uploading || pending || committing) return;
    setError(null);
    setUploading(true);

    const fd = new FormData();
    fd.append("file", file);
    const ext = await extractFileTextAction(fd);
    if (!ext.ok) {
      setUploading(false);
      setError(ext.error);
      toast.error(m.newPosition.couldNotReadFileToast(ext.error));
      return;
    }

    // Synthetic user message: full text goes to API; rendered as a compact
    // attachment chip in the chat (via the `attachment` field). Sent as a
    // plain English instruction so the model extracts cleanly regardless of
    // the UI locale; the assistant's own reply will match the locale.
    const synthetic =
      `I've attached the job description from "${ext.data.filename}". ` +
      `Extract everything you can into the draft, then ask follow-ups for anything missing.\n\n` +
      `---\n${ext.data.text}`;

    const userMsg: LocalMsg = {
      id: `u-${Date.now()}`,
      role: "user",
      content: synthetic,
      ts: Date.now(),
      attachment: { filename: ext.data.filename, chars: ext.data.char_count },
    };
    const next = [...messages, userMsg];
    setMessages(next);
    setUploading(false);
    setPending(true);

    const wireMessages: DraftMessage[] = next.map(({ role, content }) => ({
      role,
      content,
    }));
    const res = await draftPositionAction(wireMessages, draft);
    setPending(false);

    if (!res.ok) {
      setError(res.error);
      toast.error(res.error);
      return;
    }

    setMessages((prev) => [
      ...prev,
      {
        id: `a-${Date.now()}`,
        role: "assistant",
        content: res.data.assistant_message,
        ts: Date.now(),
      },
    ]);
    setDraft(res.data.draft);
    setIsComplete(res.data.is_complete);
    setMissing(res.data.missing_required);
    setQuickReplies(res.data.quick_replies ?? []);
  }

  function onFileInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) void handleFile(f);
    // Allow re-uploading the same file
    e.target.value = "";
  }

  async function commit() {
    if (committing) return;
    setError(null);
    setCommitting(true);
    const res = await createPositionFromDraftAction(draft);
    setCommitting(false);
    if (!res.ok) {
      setError(res.error);
      toast.error(m.newPosition.couldNotCreateToast(res.error));
      return;
    }
    toast.success(m.newPosition.positionCreatedToast);
    // Drop the slot we just shipped so it doesn't sit around as a stale draft.
    if (activeId) {
      setStore((prev) => {
        const next: DraftStore = { ...prev, drafts: { ...prev.drafts } };
        delete next.drafts[activeId];
        if (next.active === activeId) next.active = null;
        persistDraftStore(draftScope, next);
        return next;
      });
    }
    router.push(`/positions/${res.data.id}`);
  }

  function patchDraft(patch: Partial<PositionDraft>) {
    setDraft((d) => {
      const next = { ...d, ...patch };
      const miss: string[] = [];
      if (!next.title || !next.title.trim()) miss.push("title");
      if (!next.description || !next.description.trim())
        miss.push("description");
      const reqs = next.requirements;
      if (!reqs || typeof reqs !== "object" || Object.keys(reqs).length === 0) {
        miss.push("requirements");
      }
      setMissing(miss);
      if (miss.length === 0 && !isComplete) setIsComplete(true);
      if (miss.length > 0 && isComplete) setIsComplete(false);
      return next;
    });
  }

  const canCommit = missing.length === 0 && !committing;
  const totalRequiredFilled = 3 - missing.length;

  return (
    <>
      {/* Left: chat */}
      <section className="flex h-full min-h-0 min-w-0 flex-col border-border border-b lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between gap-2 border-b border-border px-7 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accent-muted text-primary">
              <Sparkles className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0">
              <div className="truncate text-[12px] font-medium leading-tight">
                {m.newPosition.aiHeaderTitle}
              </div>
              <div className="font-mono text-[10px] uppercase tracking-[0.04em] text-text-faint">
                {m.newPosition.aiHeaderSub}
              </div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <DraftsSwitcher
              drafts={draftList}
              activeId={activeId}
              onSwitch={switchToDraft}
              onDelete={deleteDraft}
              disabled={pending || committing}
            />
            <button
              type="button"
              onClick={startNewDraft}
              disabled={pending || committing}
              title={m.newPosition.newDraft}
              className="inline-flex items-center gap-1 rounded-[6px] px-2 py-1 text-[11px] text-text-muted transition-colors hover:bg-bg-hover hover:text-text disabled:opacity-40"
            >
              <Plus className="h-3 w-3" />
              {m.newPosition.newDraft}
            </button>
          </div>
        </div>

        <div
          ref={listRef}
          onScroll={handleListScroll}
          onDragEnter={(e) => {
            e.preventDefault();
            setDragActive(true);
          }}
          onDragOver={(e) => {
            e.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={(e) => {
            // Only deactivate when leaving the chat panel entirely
            if (e.currentTarget.contains(e.relatedTarget as Node)) return;
            setDragActive(false);
          }}
          onDrop={(e) => {
            e.preventDefault();
            setDragActive(false);
            const f = e.dataTransfer.files?.[0];
            if (f) void handleFile(f);
          }}
          className="relative min-h-0 flex-1 overflow-y-auto px-7 py-6"
        >
          <div className="flex flex-col gap-5">
            {messages.map((msg) => (
              <Message key={msg.id} message={msg} />
            ))}
            {uploading ? (
              <PendingNote label={m.newPosition.readingFile} />
            ) : null}
            {pending ? <PendingDots /> : null}

            {/* Telegram-style answer chips for the most recent assistant turn */}
            {!pending && quickReplies.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {quickReplies.map((qr, i) => (
                  <button
                    key={`${qr.field}-${qr.value}-${i}`}
                    type="button"
                    onClick={() => pickQuickReply(qr)}
                    disabled={pending || committing}
                    className="rounded-full border border-border bg-bg-elevated px-3 py-1.5 text-[12px] font-medium text-text transition-all hover:border-accent-border hover:bg-accent-muted hover:text-primary disabled:opacity-40"
                  >
                    {qr.label}
                  </button>
                ))}
              </div>
            ) : null}

            {/* Empty-state file CTA: only show before any user reply */}
            {messages.length === 1 && !uploading && !pending ? (
              <FileDropCTA
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
              />
            ) : null}
          </div>

          {/* Drag overlay */}
          {dragActive ? (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-accent-muted/60 backdrop-blur-[2px]">
              <div className="flex items-center gap-2.5 rounded-lg border border-accent-border bg-bg-elevated px-4 py-2.5 text-[12px] font-medium text-primary">
                <Upload className="h-3.5 w-3.5" />
                {m.newPosition.dragOverlay}
              </div>
            </div>
          ) : null}

          {/* Hidden file input shared by attach button + dropzone */}
          <input
            ref={fileInputRef}
            type="file"
            accept={ACCEPT_FILES}
            onChange={onFileInputChange}
            className="hidden"
          />
        </div>

        <div className="border-t border-border p-3">
          <div className="rounded-lg border border-border bg-bg-elevated transition-colors focus-within:border-border-strong">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              placeholder={
                messages.length <= 1
                  ? m.newPosition.initialPlaceholder
                  : m.newPosition.replyPlaceholder
              }
              rows={2}
              disabled={pending || committing}
              className="block max-h-[160px] min-h-[44px] w-full resize-none border-none bg-transparent px-3 pb-1.5 pt-2.5 text-[13px] leading-[1.5] outline-none placeholder:text-text-faint"
            />
            <div className="flex items-center justify-between px-2 pb-2 pt-1">
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading || pending || committing}
                  title={m.newPosition.dropTitle}
                  className="flex h-6 w-6 items-center justify-center rounded-[4px] text-text-muted transition-colors hover:bg-bg-hover hover:text-text disabled:opacity-40"
                >
                  <Paperclip className="h-3 w-3" />
                </button>
                <span className="font-mono text-[10px] text-text-faint">
                  {uploading
                    ? m.newPosition.readingFile
                    : pending
                      ? m.newPosition.thinking
                      : m.newPosition.composerHint}
                </span>
              </div>
              <button
                type="button"
                onClick={() => void send()}
                disabled={pending || committing || uploading || !input.trim()}
                className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-2.5 py-1 text-[11px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                <CornerDownLeft className="h-3 w-3" />
                {m.chat.send}
              </button>
            </div>
          </div>
          {error ? (
            <p className="mt-2 text-[11px] text-score-low">{error}</p>
          ) : null}
        </div>
      </section>

      {/* Right: live preview */}
      <section className="flex h-full min-h-0 min-w-0 flex-col bg-background">
        <div className="flex items-center justify-between border-b border-border px-7 py-3">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.newPosition.previewLabel}
            </span>
            <span className="font-mono text-[10px] text-text-faint">
              · {m.newPosition.requiredCount(totalRequiredFilled, 3)}
            </span>
          </div>
          <button
            type="button"
            onClick={commit}
            disabled={!canCommit}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-[6px] px-3 py-1.5 text-[12px] font-medium transition-all",
              canCommit
                ? "bg-primary text-primary-foreground hover:opacity-90"
                : "bg-bg-active text-text-faint",
            )}
          >
            <Check className="h-3.5 w-3.5" />
            {committing
              ? m.newPosition.creating
              : canCommit
                ? m.newPosition.create
                : m.newPosition.notReady}
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-7 py-6">
          <DraftCard
            draft={draft}
            missing={missing}
            onPatch={patchDraft}
            availableFolders={availableFolders}
          />
        </div>
      </section>
    </>
  );
}

function Message({ message }: { message: LocalMsg }) {
  const { m } = useI18n();
  const isUser = message.role === "user";
  return (
    <div className="flex flex-col gap-1.5">
      <span
        className={cn(
          "text-[11px] font-semibold",
          isUser ? "text-text" : "text-primary",
        )}
      >
        {isUser ? m.chat.you : m.chat.assistant}
      </span>
      {message.attachment ? (
        <AttachmentChip
          filename={message.attachment.filename}
          chars={message.attachment.chars}
        />
      ) : isUser ? (
        <div className="whitespace-pre-wrap text-[13px] leading-[1.6] text-text-muted">
          {message.content}
        </div>
      ) : (
        <div className="text-text">
          <RichText text={message.content} />
        </div>
      )}
    </div>
  );
}

function AttachmentChip({
  filename,
  chars,
}: {
  filename: string;
  chars: number;
}) {
  const { m } = useI18n();
  return (
    <div className="inline-flex w-fit max-w-full items-center gap-2 rounded-lg border border-border bg-bg-elevated px-3 py-2">
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-bg text-text-muted">
        <FileText className="h-3.5 w-3.5" />
      </span>
      <div className="flex min-w-0 flex-col">
        <span className="truncate font-mono text-[12px] text-text">
          {filename}
        </span>
        <span className="font-mono text-[10px] tracking-[0.04em] text-text-faint">
          {m.newPosition.fileSentToAi(formatChars(chars))}
        </span>
      </div>
    </div>
  );
}

function FileDropCTA({
  onClick,
  disabled,
}: {
  onClick: () => void;
  disabled: boolean;
}) {
  const { m } = useI18n();
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="group flex items-center gap-3 rounded-lg border border-dashed border-border bg-bg-elevated/40 px-4 py-3 text-left transition-colors hover:border-accent-border hover:bg-accent-muted disabled:opacity-50"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[8px] border border-border bg-bg text-text-muted group-hover:border-accent-border group-hover:text-primary">
        <Upload className="h-4 w-4" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-[13px] font-medium text-text">
          {m.newPosition.dropTitle}
        </span>
        <span className="text-[11px] text-text-muted">
          {m.newPosition.dropSubtitle}
        </span>
      </div>
    </button>
  );
}

function PendingNote({ label }: { label: string }) {
  const { m } = useI18n();
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[11px] font-semibold text-primary">
        {m.chat.assistant}
      </span>
      <span className="font-mono text-[11px] text-text-faint">{label}</span>
    </div>
  );
}

function PendingDots() {
  const { m } = useI18n();
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[11px] font-semibold text-primary">
        {m.chat.assistant}
      </span>
      <span className="inline-flex items-center gap-1 py-1">
        <span
          className="h-1.5 w-1.5 rounded-full bg-text-muted"
          style={{ animation: "typing 1.4s ease-in-out infinite" }}
        />
        <span
          className="h-1.5 w-1.5 rounded-full bg-text-muted"
          style={{ animation: "typing 1.4s ease-in-out 0.2s infinite" }}
        />
        <span
          className="h-1.5 w-1.5 rounded-full bg-text-muted"
          style={{ animation: "typing 1.4s ease-in-out 0.4s infinite" }}
        />
      </span>
    </div>
  );
}

function DraftCard({
  draft,
  missing,
  onPatch,
  availableFolders,
}: {
  draft: PositionDraft;
  missing: string[];
  onPatch: (patch: Partial<PositionDraft>) => void;
  availableFolders: string[];
}) {
  const { m } = useI18n();
  const openLabel = (m.status as Record<string, string>)["open"] ?? "open";
  const REQUIRED_LABEL: Record<string, string> = {
    title: m.newPosition.fieldTitle,
    description: m.newPosition.fieldDescription,
    requirements: m.newPosition.fieldRequirements,
  };
  return (
    <div className="rounded-xl border border-border bg-bg-elevated p-7">
      <div className="mb-4 flex items-center justify-between gap-2">
        <StatusBadge status="open" label={openLabel} />
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
          {m.newPosition.editablePreview}
        </span>
      </div>

      <EditableHeading
        value={draft.title}
        placeholder={m.newPosition.placeholderTitle}
        onChange={(v) => onPatch({ title: v })}
      />

      <EditableParagraph
        value={draft.description}
        placeholder={m.newPosition.placeholderDescription}
        onChange={(v) => onPatch({ description: v })}
      />

      <div className="mt-5">
        <div className="mb-2 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {m.newPosition.requirementsLabel}
        </div>
        <RequirementsEditor
          value={draft.requirements ?? null}
          onChange={(v) => onPatch({ requirements: v })}
        />
      </div>

      <div className="mt-6 grid grid-cols-1 gap-2 border-t border-border pt-5 sm:grid-cols-2">
        <SelectSoftField
          icon={<Briefcase className="h-3 w-3" />}
          label={m.newPosition.fieldSeniority}
          value={draft.seniority}
          onChange={(v) => onPatch({ seniority: v })}
          options={SENIORITY_OPTIONS(m)}
        />
        <SelectSoftField
          icon={<Globe className="h-3 w-3" />}
          label={m.newPosition.fieldWorkFormat}
          value={draft.work_format}
          onChange={(v) => onPatch({ work_format: v })}
          options={WORK_FORMAT_OPTIONS(m)}
        />
        <EditableSoftField
          icon={<MapPin className="h-3 w-3" />}
          label={m.newPosition.fieldLocation}
          value={draft.location}
          onChange={(v) => onPatch({ location: v })}
          placeholder={m.newPosition.placeholderLocation}
        />
        <SelectSoftField
          icon={<Briefcase className="h-3 w-3" />}
          label={m.newPosition.fieldEmployment}
          value={draft.employment_type}
          onChange={(v) => onPatch({ employment_type: v })}
          options={EMPLOYMENT_OPTIONS(m)}
        />
        <SalarySoftField
          icon={<DollarSign className="h-3 w-3" />}
          label={m.newPosition.fieldSalary}
          value={draft.salary_range}
          onChange={(v) => onPatch({ salary_range: v })}
        />
        <SelectSoftField
          icon={<Users className="h-3 w-3" />}
          label={m.newPosition.fieldTeamSize}
          value={draft.team_size}
          onChange={(v) => onPatch({ team_size: v })}
          options={TEAM_SIZE_OPTIONS}
        />
        <FolderSoftField
          value={draft.folder}
          onChange={(v) => onPatch({ folder: v })}
          options={availableFolders}
        />
      </div>

      {missing.length > 0 ? (
        <div className="mt-6 rounded-lg border border-dashed border-border px-4 py-3">
          <div className="mb-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {m.newPosition.stillNeeded}
          </div>
          <ul className="flex flex-wrap gap-1.5">
            {missing.map((mk) => (
              <li
                key={mk}
                className="rounded-[4px] bg-bg-active px-1.5 py-[3px] font-mono text-[10px] uppercase tracking-[0.04em] text-text-muted"
              >
                {REQUIRED_LABEL[mk] ?? mk}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="mt-6 flex items-center gap-2 rounded-lg border border-accent-border bg-accent-muted px-4 py-3 text-[12px] text-primary">
          <Check className="h-3.5 w-3.5" />
          {m.newPosition.readyMsg}
        </div>
      )}
    </div>
  );
}

// ---- inline editing primitives ----

function EditableHeading({
  value,
  placeholder,
  onChange,
}: {
  value: string | null;
  placeholder: string;
  onChange: (v: string | null) => void;
}) {
  const { m } = useI18n();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");

  function save() {
    const v = draft.trim();
    onChange(v ? v : null);
    setEditing(false);
  }

  if (editing) {
    return (
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            save();
          } else if (e.key === "Escape") {
            setDraft(value ?? "");
            setEditing(false);
          }
        }}
        placeholder={placeholder}
        className="block w-full rounded-[6px] border border-border-strong bg-bg px-3 py-1.5 text-[22px] font-semibold tracking-[-0.025em] outline-none placeholder:text-text-faint"
      />
    );
  }
  return (
    <button
      type="button"
      onClick={() => {
        setDraft(value ?? "");
        setEditing(true);
      }}
      className={cn(
        "group -mx-2 -my-1 flex w-full items-start gap-2 rounded-[6px] px-2 py-1 text-left transition-colors hover:bg-bg-hover",
        !value && "italic",
      )}
      title={m.newPosition.clickToEdit}
    >
      <span
        className={cn(
          "min-w-0 flex-1 text-[22px] font-semibold tracking-[-0.025em]",
          value ? "text-text" : "text-text-faint",
        )}
      >
        {value || placeholder}
      </span>
      <Pencil className="mt-2 h-3 w-3 shrink-0 text-text-faint opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

function EditableParagraph({
  value,
  placeholder,
  onChange,
}: {
  value: string | null;
  placeholder: string;
  onChange: (v: string | null) => void;
}) {
  const { m } = useI18n();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");

  function save() {
    const v = draft.trim();
    onChange(v ? v : null);
    setEditing(false);
  }

  if (editing) {
    return (
      <textarea
        autoFocus
        rows={4}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            save();
          } else if (e.key === "Escape") {
            setDraft(value ?? "");
            setEditing(false);
          }
        }}
        placeholder={placeholder}
        className="mt-2 block w-full max-w-[640px] resize-y rounded-[6px] border border-border-strong bg-bg px-3 py-2 text-[13px] leading-[1.6] outline-none placeholder:text-text-faint"
      />
    );
  }
  return (
    <button
      type="button"
      onClick={() => {
        setDraft(value ?? "");
        setEditing(true);
      }}
      className="group mt-2 block w-full max-w-[640px] -mx-2 rounded-[6px] px-2 py-1 text-left transition-colors hover:bg-bg-hover"
      title={m.newPosition.clickToEdit}
    >
      <p
        className={cn(
          "text-[13px] leading-[1.6]",
          value ? "text-text-muted" : "italic text-text-faint",
        )}
      >
        {value || placeholder}
      </p>
    </button>
  );
}

function RequirementsEditor({
  value,
  onChange,
}: {
  value: Record<string, unknown> | null;
  onChange: (v: Record<string, unknown> | null) => void;
}) {
  const { m } = useI18n();
  const entries = Object.entries(value ?? {});
  const [adding, setAdding] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

  function remove(key: string) {
    const next = { ...(value ?? {}) };
    delete next[key];
    onChange(Object.keys(next).length ? next : null);
  }

  function addNew() {
    const k = newKey.trim();
    if (!k) {
      setAdding(false);
      setNewKey("");
      setNewValue("");
      return;
    }
    onChange({ ...(value ?? {}), [k]: newValue.trim() || "required" });
    setNewKey("");
    setNewValue("");
    setAdding(false);
  }

  function saveEdit() {
    if (editingKey == null) return;
    onChange({ ...(value ?? {}), [editingKey]: editValue.trim() || "required" });
    setEditingKey(null);
    setEditValue("");
  }

  return (
    <ul className="flex flex-wrap items-center gap-1.5">
      {entries.map(([k, v]) =>
        editingKey === k ? (
          <li key={k} className="flex items-center gap-1 rounded-[4px] border border-border-strong bg-bg px-1.5 py-[3px]">
            <span className="font-mono text-[10.5px] text-text">{k}</span>
            <span className="font-mono text-[10.5px] text-text-faint">·</span>
            <input
              autoFocus
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              onBlur={saveEdit}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  saveEdit();
                } else if (e.key === "Escape") {
                  setEditingKey(null);
                  setEditValue("");
                }
              }}
              className="w-[120px] border-none bg-transparent font-mono text-[10.5px] tabular-nums text-text outline-none"
            />
          </li>
        ) : (
          <li
            key={k}
            className="group flex items-center gap-1 rounded-[4px] border border-border bg-bg px-2 py-1 font-mono text-[10.5px] tabular-nums text-text-muted transition-colors hover:border-border-strong"
          >
            <button
              type="button"
              onClick={() => {
                setEditingKey(k);
                setEditValue(String(v));
              }}
              className="flex items-center gap-1"
              title={m.newPosition.editValue}
            >
              <span className="text-text">{k}</span>
              <span className="text-text-faint">·</span>
              <span>{String(v)}</span>
            </button>
            <button
              type="button"
              onClick={() => remove(k)}
              className="ml-0.5 -mr-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-[3px] text-text-faint opacity-0 transition-all hover:bg-bg-active hover:text-text group-hover:opacity-100"
              title={m.newPosition.removeReq}
            >
              <X className="h-2.5 w-2.5" />
            </button>
          </li>
        ),
      )}

      {adding ? (
        <li className="flex items-center gap-1 rounded-[4px] border border-border-strong bg-bg px-1.5 py-[3px]">
          <input
            autoFocus
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Tab" || e.key === "Enter") {
                e.preventDefault();
                if (newKey.trim()) {
                  // Move focus to the value field rather than save
                  (
                    e.currentTarget.nextElementSibling
                      ?.nextElementSibling as HTMLInputElement | null
                  )?.focus();
                } else {
                  setAdding(false);
                }
              } else if (e.key === "Escape") {
                setAdding(false);
                setNewKey("");
                setNewValue("");
              }
            }}
            placeholder={m.newPosition.keyPlaceholder}
            className="w-[80px] border-none bg-transparent font-mono text-[10.5px] text-text placeholder:text-text-faint outline-none"
          />
          <span className="font-mono text-[10.5px] text-text-faint">·</span>
          <input
            value={newValue}
            onChange={(e) => setNewValue(e.target.value)}
            onBlur={addNew}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addNew();
              } else if (e.key === "Escape") {
                setAdding(false);
                setNewKey("");
                setNewValue("");
              }
            }}
            placeholder={m.newPosition.valuePlaceholder}
            className="w-[100px] border-none bg-transparent font-mono text-[10.5px] tabular-nums text-text placeholder:text-text-faint outline-none"
          />
        </li>
      ) : (
        <li>
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="flex items-center gap-1 rounded-[4px] border border-dashed border-border px-2 py-1 font-mono text-[10.5px] text-text-faint transition-colors hover:border-border-strong hover:text-text-muted"
          >
            <Plus className="h-2.5 w-2.5" />
            {m.newPosition.addReq}
          </button>
        </li>
      )}

      {entries.length === 0 && !adding ? (
        <li className="text-[12px] italic text-text-faint">
          {m.newPosition.placeholderRequirements}
        </li>
      ) : null}
    </ul>
  );
}

function EditableSoftField({
  icon,
  label,
  value,
  placeholder,
  onChange,
}: {
  icon: React.ReactNode;
  label: string;
  value: string | null;
  placeholder: string;
  onChange: (v: string | null) => void;
}) {
  const { m } = useI18n();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");

  function save() {
    const v = draft.trim();
    onChange(v ? v : null);
    setEditing(false);
  }

  if (editing) {
    return (
      <div className="flex items-center gap-2 rounded-[6px] border border-border-strong bg-bg px-2 py-1.5">
        <span className="flex h-3 w-3 items-center justify-center text-text-faint">
          {icon}
        </span>
        <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {label}
        </span>
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              save();
            } else if (e.key === "Escape") {
              setDraft(value ?? "");
              setEditing(false);
            }
          }}
          placeholder={placeholder}
          className="ml-auto min-w-0 flex-1 truncate border-none bg-transparent text-right text-[12px] text-text placeholder:text-text-faint outline-none"
        />
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={() => {
        setDraft(value ?? "");
        setEditing(true);
      }}
      className="group flex items-baseline gap-2 rounded-[6px] px-2 py-1.5 text-left transition-colors hover:bg-bg-hover"
      title={m.newPosition.clickToEdit}
    >
      <span className="flex h-3 w-3 items-center justify-center text-text-faint">
        {icon}
      </span>
      <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
        {label}
      </span>
      <span
        className={cn(
          "ml-auto truncate text-[12px]",
          value ? "text-text" : "italic text-text-faint",
        )}
      >
        {value ?? placeholder}
      </span>
      <Pencil className="h-2.5 w-2.5 shrink-0 text-text-faint opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

// ---- constrained option sets ----

/** @ui-only Position-form select option. */
type SelectOption = { value: string; label: string };

/** @ui-only Localized position-flow presentation copy. */
type Msg = ReturnType<typeof useI18n>["m"];

function SENIORITY_OPTIONS(_m: Msg): SelectOption[] {
  return [
    { value: "junior", label: "Junior" },
    { value: "middle", label: "Middle" },
    { value: "senior", label: "Senior" },
    { value: "lead", label: "Lead" },
    { value: "principal", label: "Principal" },
  ];
}

function WORK_FORMAT_OPTIONS(m: Msg): SelectOption[] {
  return [
    { value: "remote", label: m.newPosition.workFormat.remote },
    { value: "hybrid", label: m.newPosition.workFormat.hybrid },
    { value: "onsite", label: m.newPosition.workFormat.onsite },
  ];
}

function EMPLOYMENT_OPTIONS(m: Msg): SelectOption[] {
  return [
    { value: "full_time", label: m.newPosition.employment.fullTime },
    { value: "contract", label: m.newPosition.employment.contract },
    { value: "part_time", label: m.newPosition.employment.partTime },
  ];
}

const TEAM_SIZE_OPTIONS: SelectOption[] = [
  { value: "1-3", label: "1–3" },
  { value: "4-10", label: "4–10" },
  { value: "11-30", label: "11–30" },
  { value: "30+", label: "30+" },
];

// ---- constrained controls (selects + structured salary) ----

/**
 * Display-name lookup: maps a stored canonical value back to its localized label.
 * Falls back to the raw value when the model emitted something off-list.
 */
function findLabel(options: SelectOption[], value: string | null): string | null {
  if (value == null) return null;
  return options.find((o) => o.value === value)?.label ?? value;
}

function SelectSoftField({
  icon,
  label,
  value,
  options,
  onChange,
}: {
  icon: React.ReactNode;
  label: string;
  value: string | null;
  options: SelectOption[];
  onChange: (v: string | null) => void;
}) {
  const { m } = useI18n();
  const [open, setOpen] = useState(false);
  const display = findLabel(options, value);
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="group flex w-full items-baseline gap-2 rounded-[6px] px-2 py-1.5 text-left transition-colors hover:bg-bg-hover"
      >
        <span className="flex h-3 w-3 items-center justify-center text-text-faint">
          {icon}
        </span>
        <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {label}
        </span>
        <span
          className={cn(
            "ml-auto truncate text-[12px]",
            display ? "text-text" : "italic text-text-faint",
          )}
        >
          {display ?? m.newPosition.choosePlaceholder}
        </span>
        <Pencil className="h-2.5 w-2.5 shrink-0 text-text-faint opacity-0 transition-opacity group-hover:opacity-100" />
      </button>
      {open ? (
        <>
          <div
            className="fixed inset-0 z-10"
            onClick={() => setOpen(false)}
          />
          <div className="absolute right-0 top-full z-20 mt-1 flex min-w-[160px] flex-col gap-px rounded-[6px] border border-border bg-bg-elevated p-1 shadow-lg">
            {options.map((opt) => {
              const active = value === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => {
                    onChange(active ? null : opt.value);
                    setOpen(false);
                  }}
                  className={cn(
                    "rounded-[4px] px-2 py-1.5 text-left text-[12px] transition-colors",
                    active
                      ? "bg-accent-muted text-primary"
                      : "text-text-muted hover:bg-bg-hover hover:text-text",
                  )}
                >
                  {opt.label}
                </button>
              );
            })}
            {value != null ? (
              <button
                type="button"
                onClick={() => {
                  onChange(null);
                  setOpen(false);
                }}
                className="mt-1 border-t border-border pt-1.5 text-left text-[11px] text-text-faint hover:text-text-muted"
              >
                {m.newPosition.clearChoice}
              </button>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}

/**
 * Folder picker — popover lists existing folders and lets the user type a
 * new name, all consistent with the dashboard's folder filter.
 */
function FolderSoftField({
  value,
  options,
  onChange,
}: {
  value: string | null;
  options: string[];
  onChange: (v: string | null) => void;
}) {
  const { m } = useI18n();
  const [open, setOpen] = useState(false);
  const [newName, setNewName] = useState("");

  function commitNew() {
    const v = newName.trim();
    if (v) {
      onChange(v);
      setOpen(false);
      setNewName("");
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="group flex w-full items-baseline gap-2 rounded-[6px] px-2 py-1.5 text-left transition-colors hover:bg-bg-hover"
      >
        <span className="flex h-3 w-3 items-center justify-center text-text-faint">
          <Folder className="h-3 w-3" />
        </span>
        <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {m.newPosition.fieldFolder}
        </span>
        <span
          className={cn(
            "ml-auto truncate text-[12px]",
            value ? "text-text" : "italic text-text-faint",
          )}
        >
          {value ?? m.newPosition.folderPlaceholder}
        </span>
        <Pencil className="h-2.5 w-2.5 shrink-0 text-text-faint opacity-0 transition-opacity group-hover:opacity-100" />
      </button>
      {open ? (
        <>
          <div
            className="fixed inset-0 z-10"
            onClick={() => setOpen(false)}
          />
          <div className="absolute right-0 top-full z-20 mt-1 flex w-[220px] flex-col gap-px rounded-[6px] border border-border bg-bg-elevated p-1 shadow-lg">
            {options.length > 0
              ? options.map((opt) => {
                  const active = value === opt;
                  return (
                    <button
                      key={opt}
                      type="button"
                      onClick={() => {
                        onChange(active ? null : opt);
                        setOpen(false);
                      }}
                      className={cn(
                        "rounded-[4px] px-2 py-1.5 text-left text-[12px] transition-colors",
                        active
                          ? "bg-accent-muted text-primary"
                          : "text-text-muted hover:bg-bg-hover hover:text-text",
                      )}
                    >
                      {opt}
                    </button>
                  );
                })
              : null}
            <div className="mt-1 border-t border-border pt-1.5">
              <input
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitNew();
                  }
                  if (e.key === "Escape") setOpen(false);
                }}
                placeholder={m.newPosition.folderNewPlaceholder}
                className="w-full rounded-[4px] border border-border bg-bg px-2 py-1 text-[11.5px] outline-none placeholder:text-text-faint focus:border-border-strong"
              />
            </div>
            {value != null ? (
              <button
                type="button"
                onClick={() => {
                  onChange(null);
                  setOpen(false);
                }}
                className="mt-1 border-t border-border pt-1.5 text-left text-[11px] text-text-faint hover:text-text-muted"
              >
                {m.newPosition.clearChoice}
              </button>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}

/**
 * Salary input enforced as a from–to range in сомони/мес net. Always serializes
 * to the canonical string "<from>-<to> сомони/мес net" (or "от <from>" / "до <to>"
 * if one side is open) so the engine sees a consistent format.
 */
function SalarySoftField({
  icon,
  label,
  value,
  onChange,
}: {
  icon: React.ReactNode;
  label: string;
  value: string | null;
  onChange: (v: string | null) => void;
}) {
  const { m } = useI18n();
  const parsed = parseSalary(value);
  const [editing, setEditing] = useState(false);
  const [from, setFrom] = useState<string>(parsed.from?.toString() ?? "");
  const [to, setTo] = useState<string>(parsed.to?.toString() ?? "");

  function save() {
    const f = from.trim() === "" ? null : Number(from.replace(/\D/g, ""));
    const t = to.trim() === "" ? null : Number(to.replace(/\D/g, ""));
    onChange(formatSalary(f, t));
    setEditing(false);
  }

  if (editing) {
    return (
      <div className="flex items-center gap-2 rounded-[6px] border border-border-strong bg-bg px-2 py-1.5">
        <span className="flex h-3 w-3 items-center justify-center text-text-faint">
          {icon}
        </span>
        <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {label}
        </span>
        <input
          inputMode="numeric"
          autoFocus
          value={from}
          onChange={(e) => setFrom(e.target.value.replace(/\D/g, ""))}
          onBlur={(e) => {
            // Don't commit (and close) when moving into the sibling "to" input;
            // only save when focus leaves the field entirely. Prevents losing a
            // typed minimum on click-away.
            if ((e.relatedTarget as HTMLElement | null)?.tagName === "INPUT") return;
            save();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              save();
            } else if (e.key === "Escape") {
              setEditing(false);
              setFrom(parsed.from?.toString() ?? "");
              setTo(parsed.to?.toString() ?? "");
            }
          }}
          placeholder={m.newPosition.salaryFromPlaceholder}
          className="ml-auto w-[64px] border-none bg-transparent text-right text-[12px] tabular-nums text-text outline-none placeholder:text-text-faint"
        />
        <span className="text-[11px] text-text-faint">–</span>
        <input
          inputMode="numeric"
          value={to}
          onChange={(e) => setTo(e.target.value.replace(/\D/g, ""))}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              save();
            } else if (e.key === "Escape") {
              setEditing(false);
              setFrom(parsed.from?.toString() ?? "");
              setTo(parsed.to?.toString() ?? "");
            }
          }}
          placeholder={m.newPosition.salaryToPlaceholder}
          className="w-[64px] border-none bg-transparent text-right text-[12px] tabular-nums text-text outline-none placeholder:text-text-faint"
        />
        <span className="text-[10px] text-text-faint">{m.newPosition.salarySuffix}</span>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={() => {
        setFrom(parsed.from?.toString() ?? "");
        setTo(parsed.to?.toString() ?? "");
        setEditing(true);
      }}
      className="group flex items-baseline gap-2 rounded-[6px] px-2 py-1.5 text-left transition-colors hover:bg-bg-hover"
    >
      <span className="flex h-3 w-3 items-center justify-center text-text-faint">
        {icon}
      </span>
      <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
        {label}
      </span>
      <span
        className={cn(
          "ml-auto truncate text-[12px]",
          value ? "text-text" : "italic text-text-faint",
        )}
      >
        {formatSalaryDisplay(parsed, m)}
      </span>
      <Pencil className="h-2.5 w-2.5 shrink-0 text-text-faint opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

/** @ui-only Salary values parsed from form inputs. */
type ParsedSalary = { from: number | null; to: number | null };

function parseSalary(raw: string | null): ParsedSalary {
  if (!raw) return { from: null, to: null };
  // Pick out runs of digits, ignoring k/К markers we may have stored
  const cleaned = raw.replace(/[\s ]/g, "");
  const matches = [...cleaned.matchAll(/(\d{2,7})(к|k|тыс)?/gi)];
  const nums = matches.map((m) => {
    const base = Number(m[1]);
    const isThousand = !!m[2];
    return isThousand ? base * 1000 : base;
  });
  if (nums.length === 0) return { from: null, to: null };
  if (nums.length === 1) {
    if (/^от/i.test(cleaned)) return { from: nums[0], to: null };
    if (/^до/i.test(cleaned)) return { from: null, to: nums[0] };
    return { from: nums[0], to: null };
  }
  return { from: nums[0], to: nums[1] };
}

function formatSalary(from: number | null, to: number | null): string | null {
  if (from == null && to == null) return null;
  if (from != null && to != null) return `${from}-${to} сомони/мес net`;
  if (from != null) return `от ${from} сомони/мес net`;
  return `до ${to} сомони/мес net`;
}

function formatSalaryDisplay(p: ParsedSalary, m: Msg): string {
  if (p.from == null && p.to == null) return m.newPosition.salaryPlaceholderShort;
  const fmt = (n: number) => n.toLocaleString("ru-RU");
  if (p.from != null && p.to != null) {
    return `${fmt(p.from)}–${fmt(p.to)} ${m.newPosition.salarySuffix}`;
  }
  if (p.from != null)
    return `${m.newPosition.salaryFromPlaceholder} ${fmt(p.from)} ${m.newPosition.salarySuffix}`;
  return `${m.newPosition.salaryToPlaceholder} ${fmt(p.to as number)} ${m.newPosition.salarySuffix}`;
}

// ---- drafts switcher ----

function DraftsSwitcher({
  drafts,
  activeId,
  onSwitch,
  onDelete,
  disabled,
}: {
  drafts: DraftSlot[];
  activeId: string | null;
  onSwitch: (id: string) => void;
  onDelete: (id: string) => void;
  disabled?: boolean;
}) {
  const { m } = useI18n();
  const [open, setOpen] = useState(false);
  const active = activeId ? drafts.find((d) => d.id === activeId) ?? null : null;
  const count = drafts.length;
  const label = active
    ? active.name || m.newPosition.untitledDraft
    : m.newPosition.draftsLabel;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={disabled || count === 0}
        title={m.newPosition.draftsLabel}
        className="inline-flex max-w-[200px] items-center gap-1 rounded-[6px] border border-border bg-bg-elevated px-2 py-1 text-[11px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover disabled:opacity-40"
      >
        <Files className="h-3 w-3 shrink-0 text-text-muted" />
        <span className="truncate">{label}</span>
        {count > 0 ? (
          <span className="font-mono text-[10px] text-text-faint">· {count}</span>
        ) : null}
        <ChevronDown className="h-3 w-3 shrink-0 text-text-faint" />
      </button>
      {open ? (
        <>
          <div
            className="fixed inset-0 z-10"
            onClick={() => setOpen(false)}
          />
          <div className="absolute right-0 top-full z-20 mt-1 flex max-h-[60vh] w-[280px] flex-col overflow-y-auto rounded-[6px] border border-border bg-bg-elevated p-1 shadow-lg">
            {drafts.length === 0 ? (
              <div className="px-2 py-3 text-center text-[11px] text-text-faint">
                {m.newPosition.noDrafts}
              </div>
            ) : (
              drafts.map((d) => {
                const isActive = d.id === activeId;
                return (
                  <div
                    key={d.id}
                    className={cn(
                      "group flex items-center gap-1 rounded-[4px] px-1.5 py-1 transition-colors",
                      isActive ? "bg-bg-active" : "hover:bg-bg-hover",
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => {
                        onSwitch(d.id);
                        setOpen(false);
                      }}
                      className="flex min-w-0 flex-1 flex-col items-start text-left"
                    >
                      <span
                        className={cn(
                          "w-full truncate text-[12px]",
                          isActive ? "text-text" : "text-text-muted",
                        )}
                      >
                        {d.name || m.newPosition.untitledDraft}
                      </span>
                      <span className="font-mono text-[10px] text-text-faint">
                        {m.format.relativeTime(new Date(d.updatedAt).toISOString())}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDelete(d.id);
                      }}
                      title={m.newPosition.deleteDraft}
                      className="flex h-5 w-5 shrink-0 items-center justify-center rounded-[3px] text-text-faint opacity-0 transition-all hover:bg-bg-active hover:text-score-low group-hover:opacity-100"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </>
      ) : null}
    </div>
  );
}
