"use client";

import { useMemo, useState, useTransition } from "react";
import {
  Clock3,
  Loader2,
  Mail,
  MessageCircle,
  Plus,
  RefreshCw,
  Send,
  ShieldCheck,
} from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Collapsible } from "@/components/ui/collapsible";
import {
  cancelCommunicationMessageAction,
  createCandidateContactPointAction,
  listCandidateCommunicationThreadsAction,
  listCommunicationMessagesAction,
  previewCandidateCommunicationAction,
  sendCandidateCommunicationAction,
  updateCandidateContactPointAction,
} from "@/lib/actions";
import type {
  CandidateCommunicationPreview,
  CandidateContactPoint,
  CommunicationAccount,
  CommunicationMessage,
  CommunicationTemplate,
  CommunicationThread,
  CreatableContactChannel,
  OutboundMessageChannel,
} from "@/lib/types";
import {
  isCommunicationAccountAvailable,
  isSandboxAccount,
} from "@/lib/communication-capabilities";
import { cn } from "@/lib/utils";

/** @ui-only Select-option projection for candidate communications. */
type PositionOption = { id: string; title: string };
const fieldControlClass =
  "w-full rounded-[6px] border border-border bg-bg px-2.5 py-2 text-[11px] text-text outline-none focus:border-border-strong";

function newIdempotencyKey(prefix: string): string {
  const id =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${id}`;
}

function contactSupportsChannel(
  contact: CandidateContactPoint,
  channel: OutboundMessageChannel,
): boolean {
  return contact.channel === channel;
}

export function CandidateCommunicationsPanel({
  candidateId,
  positions,
  initialContacts,
  initialAccounts,
  initialTemplates,
  initialThreads,
  initialLoadFailed,
  canManageContacts,
  canSend,
  canReadHistory,
}: {
  candidateId: string;
  positions: PositionOption[];
  initialContacts: CandidateContactPoint[];
  initialAccounts: CommunicationAccount[];
  initialTemplates: CommunicationTemplate[];
  initialThreads: CommunicationThread[];
  initialLoadFailed: boolean;
  canManageContacts: boolean;
  canSend: boolean;
  canReadHistory: boolean;
}) {
  const { locale, m } = useI18n();
  const copy = m.communications;
  const [contacts, setContacts] = useState(initialContacts);
  const [threads, setThreads] = useState(initialThreads);
  const [loadFailed, setLoadFailed] = useState(initialLoadFailed);
  const [channel, setChannel] = useState<OutboundMessageChannel>("email");
  const [contactId, setContactId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [positionId, setPositionId] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [scheduledFor, setScheduledFor] = useState("");
  const [preview, setPreview] = useState<CandidateCommunicationPreview | null>(
    null,
  );
  const [contactChannel, setContactChannel] =
    useState<CreatableContactChannel>("email");
  const [contactValue, setContactValue] = useState("");
  const [selectedThreadId, setSelectedThreadId] = useState(
    initialThreads[0]?.id ?? "",
  );
  const [messages, setMessages] = useState<CommunicationMessage[]>([]);
  const [messagesCursor, setMessagesCursor] = useState<string | null>(null);
  const [contactsPending, startContacts] = useTransition();
  const [previewPending, startPreview] = useTransition();
  const [sendPending, startSend] = useTransition();
  const [historyPending, startHistory] = useTransition();

  const channelContacts = useMemo(
    () =>
      contacts.filter(
        (contact) =>
          contactSupportsChannel(contact, channel) && !contact.is_opted_out,
      ),
    [channel, contacts],
  );
  const channelAccounts = useMemo(
    () =>
      initialAccounts.filter(
        (account) => account.channel === channel && account.is_active,
      ),
    [channel, initialAccounts],
  );
  const channelTemplates = useMemo(
    () =>
      initialTemplates.filter(
        (template) => template.channel === channel && template.is_active,
      ),
    [channel, initialTemplates],
  );
  const availableChannelAccounts = useMemo(
    () => channelAccounts.filter(isCommunicationAccountAvailable),
    [channelAccounts],
  );

  const resolvedContactId =
    channelContacts.find((contact) => contact.id === contactId)?.id ??
    channelContacts.find((contact) => contact.is_primary)?.id ??
    channelContacts[0]?.id ??
    "";
  const resolvedAccountId =
    availableChannelAccounts.find((account) => account.id === accountId)?.id ??
    availableChannelAccounts[0]?.id ??
    "";
  const selectedAccount = channelAccounts.find(
    (account) => account.id === resolvedAccountId,
  );
  const accountProviders = useMemo(
    () => new Map(initialAccounts.map((account) => [account.id, account.provider])),
    [initialAccounts],
  );
  const selectedTemplate = channelTemplates.find(
    (template) => template.id === templateId,
  );

  function composePayload() {
    return {
      channel,
      contact_point_id: resolvedContactId || null,
      account_id: resolvedAccountId || null,
      template_id: selectedTemplate?.id ?? null,
      position_id: positionId || null,
      subject: selectedTemplate ? null : subject.trim() || null,
      body: selectedTemplate ? null : body.trim() || null,
    };
  }

  function addContact() {
    const value = contactValue.trim();
    if (!value || contactsPending) return;
    startContacts(async () => {
      const result = await createCandidateContactPointAction(candidateId, {
        channel: contactChannel,
        value,
        is_primary: !contacts.some(
          (contact) => contact.channel === contactChannel,
        ),
      });
      if (!result.ok) {
        toast.error(`${copy.contactFailed}: ${result.error}`);
        return;
      }
      setContacts((current) => [...current, result.data]);
      setContactValue("");
      toast.success(copy.contactSaved);
    });
  }

  function toggleOptOut(contact: CandidateContactPoint) {
    startContacts(async () => {
      const result = await updateCandidateContactPointAction(
        candidateId,
        contact.id,
        { is_opted_out: !contact.is_opted_out },
      );
      if (!result.ok) {
        toast.error(`${copy.contactFailed}: ${result.error}`);
        return;
      }
      setContacts((current) =>
        current.map((item) => (item.id === result.data.id ? result.data : item)),
      );
      toast.success(copy.contactSaved);
    });
  }

  function preparePreview() {
    if (!resolvedContactId || !resolvedAccountId || previewPending) return;
    startPreview(async () => {
      const result = await previewCandidateCommunicationAction(
        candidateId,
        composePayload(),
      );
      if (!result.ok) {
        setPreview(null);
        toast.error(`${copy.previewFailed}: ${result.error}`);
        return;
      }
      setPreview(result.data);
    });
  }

  async function refreshThreads() {
    const result = await listCandidateCommunicationThreadsAction(candidateId);
    if (!result.ok) {
      setLoadFailed(true);
      return;
    }
    setThreads(result.data);
    setLoadFailed(false);
    if (!selectedThreadId && result.data[0]) {
      setSelectedThreadId(result.data[0].id);
    }
  }

  function sendMessage() {
    if (!resolvedContactId || !resolvedAccountId || sendPending) return;
    startSend(async () => {
      const result = await sendCandidateCommunicationAction(candidateId, {
        ...composePayload(),
        idempotency_key: newIdempotencyKey("candidate-message"),
        scheduled_for: scheduledFor
          ? new Date(scheduledFor).toISOString()
          : null,
      });
      if (!result.ok) {
        toast.error(`${copy.sendFailed}: ${result.error}`);
        return;
      }
      toast.success(
        isSandboxAccount(selectedAccount)
          ? scheduledFor
            ? copy.sandboxScheduled
            : copy.sandboxRecorded
          : scheduledFor
            ? copy.scheduled
            : copy.sent,
      );
      setPreview(null);
      setBody("");
      setSubject("");
      setScheduledFor("");
      await refreshThreads();
      await openThread(result.data.thread_id);
    });
  }

  async function openThread(threadId: string, cursor?: string | null) {
    setSelectedThreadId(threadId);
    startHistory(async () => {
      const result = await listCommunicationMessagesAction(threadId, cursor);
      if (!result.ok) {
        toast.error(copy.loadFailed);
        return;
      }
      setMessages((current) =>
        cursor
          ? uniqueMessages([...current, ...result.data.items])
          : result.data.items,
      );
      setMessagesCursor(result.data.next_cursor);
    });
  }

  function cancelMessage(message: CommunicationMessage) {
    startHistory(async () => {
      const result = await cancelCommunicationMessageAction(
        message.id,
        candidateId,
      );
      if (!result.ok) {
        toast.error(copy.sendFailed);
        return;
      }
      setMessages((current) =>
        current.map((item) => (item.id === result.data.id ? result.data : item)),
      );
      toast.success(copy.cancelled);
      await refreshThreads();
    });
  }

  return (
    <Collapsible
      title={copy.title}
      meta={`${contacts.length} · ${threads.length}`}
      defaultOpen={threads.length > 0}
    >
      <div className="space-y-5 pt-3">
        {canReadHistory && loadFailed ? (
          <div className="flex items-center justify-between gap-3 rounded-lg border border-score-mid/30 bg-score-mid/5 px-3 py-2 text-[11px] text-score-mid">
            <span>{copy.loadFailed}</span>
            <button
              type="button"
              onClick={() => void refreshThreads()}
              className="inline-flex items-center gap-1 font-medium"
            >
              <RefreshCw className="h-3 w-3" /> {copy.retry}
            </button>
          </div>
        ) : null}

        <section>
          <div className="mb-2 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {copy.contacts}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {contacts.map((contact) => (
              <div
                key={contact.id}
                className={cn(
                  "inline-flex items-center gap-2 rounded-[6px] border px-2.5 py-1.5 text-[10.5px]",
                  contact.is_opted_out
                    ? "border-border bg-bg text-text-faint line-through"
                    : "border-border bg-bg-elevated text-text-muted",
                )}
              >
                <span className="uppercase text-[9px] tracking-wide text-text-faint">
                  {contact.channel}
                </span>
                <span className="max-w-[220px] truncate">{contact.value}</span>
                {contact.is_primary ? (
                  <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[9px] text-primary">
                    {copy.primary}
                  </span>
                ) : null}
                {canManageContacts ? (
                  <button
                    type="button"
                    disabled={contactsPending}
                    onClick={() => toggleOptOut(contact)}
                    className="text-[9.5px] text-text-faint hover:text-text"
                  >
                    {contact.is_opted_out ? copy.restore : copy.optOut}
                  </button>
                ) : null}
              </div>
            ))}
          </div>
          {canManageContacts ? (
            <div className="mt-2 grid gap-2 sm:grid-cols-[130px_1fr_auto]">
              <select
                value={contactChannel}
                onChange={(event) =>
                  setContactChannel(event.target.value as CreatableContactChannel)
                }
                aria-label={copy.contactChannel}
                className="rounded-[6px] border border-border bg-bg px-2.5 py-2 text-[11px]"
              >
                <option value="email">Email</option>
                <option value="phone">Phone</option>
                <option value="telegram">Telegram</option>
              </select>
              <input
                value={contactValue}
                onChange={(event) => setContactValue(event.target.value)}
                placeholder={copy.contactValue}
                aria-label={copy.contactValue}
                className="rounded-[6px] border border-border bg-bg px-2.5 py-2 text-[11px] outline-none focus:border-border-strong"
              />
              <button
                type="button"
                onClick={addContact}
                disabled={!contactValue.trim() || contactsPending}
                className="inline-flex items-center justify-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-3 py-2 text-[11px] font-medium disabled:opacity-50"
              >
                {contactsPending ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Plus className="h-3 w-3" />
                )}
                {contactsPending ? copy.adding : copy.add}
              </button>
            </div>
          ) : null}
        </section>

        {canSend ? (
          <section className="rounded-lg border border-border bg-bg-elevated p-3">
          <div className="mb-3 flex items-center gap-2 text-[11.5px] font-medium">
            <Mail className="h-3.5 w-3.5 text-text-muted" /> {copy.compose}
          </div>
          <div className="grid gap-2 md:grid-cols-2 lg:grid-cols-4">
            <Field label={copy.channel}>
              <select
                value={channel}
                onChange={(event) => {
                  setChannel(event.target.value as OutboundMessageChannel);
                  setContactId("");
                  setAccountId("");
                  setTemplateId("");
                  setPreview(null);
                }}
                className={fieldControlClass}
              >
                <option value="email">Email</option>
                <option value="telegram">Telegram</option>
              </select>
            </Field>
            <Field label={copy.recipient}>
              <select
                value={resolvedContactId}
                onChange={(event) => setContactId(event.target.value)}
                className={fieldControlClass}
              >
                {channelContacts.length === 0 ? (
                  <option value="">{copy.noContact}</option>
                ) : null}
                {channelContacts.map((contact) => (
                  <option key={contact.id} value={contact.id}>
                    {contact.value}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={copy.account}>
              <select
                value={resolvedAccountId}
                onChange={(event) => setAccountId(event.target.value)}
                className={fieldControlClass}
              >
                {availableChannelAccounts.length === 0 ? (
                  <option value="">{copy.noAccount}</option>
                ) : null}
                {channelAccounts.map((account) => (
                  <option
                    key={account.id}
                    value={account.id}
                    disabled={!isCommunicationAccountAvailable(account)}
                  >
                    {account.name} · {account.provider}
                    {!isCommunicationAccountAvailable(account)
                      ? ` · ${copy.providerUnavailable}`
                      : ""}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={copy.template}>
              <select
                value={templateId}
                onChange={(event) => {
                  setTemplateId(event.target.value);
                  setPreview(null);
                }}
                className={fieldControlClass}
              >
                <option value="">{copy.customMessage}</option>
                {channelTemplates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.name} · {template.language.toUpperCase()}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <div className="mt-2 grid gap-2 md:grid-cols-2">
            <Field label={copy.subject}>
              <input
                value={selectedTemplate?.subject_template ?? subject}
                onChange={(event) => setSubject(event.target.value)}
                disabled={Boolean(selectedTemplate)}
                className={cn(fieldControlClass, "disabled:opacity-60")}
              />
            </Field>
            <Field label={copy.scheduledFor}>
              <input
                type="datetime-local"
                value={scheduledFor}
                onChange={(event) => setScheduledFor(event.target.value)}
                className={fieldControlClass}
              />
            </Field>
          </div>
          <Field label={copy.body} className="mt-2">
            <textarea
              rows={5}
              value={selectedTemplate?.body_template ?? body}
              onChange={(event) => setBody(event.target.value)}
              disabled={Boolean(selectedTemplate)}
              className={cn(
                fieldControlClass,
                "resize-y disabled:opacity-60",
              )}
            />
          </Field>
          {positions.length > 0 ? (
            <div className="mt-2 max-w-sm">
              <select
                value={positionId}
                onChange={(event) => setPositionId(event.target.value)}
                aria-label="Position"
                className={fieldControlClass}
              >
                <option value="">—</option>
                {positions.map((position) => (
                  <option key={position.id} value={position.id}>
                    {position.title}
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          {preview ? (
            <div className="mt-3 rounded-lg border border-primary/20 bg-primary/5 p-3">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] text-text-faint">
                <ShieldCheck className="h-3 w-3 text-primary" />
                {preview.target.masked_value} · {preview.target.account_name}
              </div>
              {preview.subject ? (
                <p className="text-[11px] font-medium text-text">
                  {preview.subject}
                </p>
              ) : null}
              <p className="mt-1 whitespace-pre-wrap text-[11px] leading-5 text-text-muted">
                {preview.body}
              </p>
            </div>
          ) : null}

          {!resolvedContactId ? (
            <p className="mt-2 text-[10.5px] text-score-mid">{copy.noContact}</p>
          ) : null}
          {!resolvedAccountId ? (
            <p className="mt-2 text-[10.5px] text-score-mid">{copy.noAccount}</p>
          ) : null}
          {isSandboxAccount(selectedAccount) ? (
            <p className="mt-2 rounded-md border border-score-mid/25 bg-score-mid/5 px-2.5 py-2 text-[10.5px] text-score-mid">
              {copy.sandboxNotice}
            </p>
          ) : null}
          <div className="mt-3 flex flex-wrap justify-end gap-2">
            <button
              type="button"
              onClick={preparePreview}
              disabled={
                previewPending ||
                !resolvedContactId ||
                !resolvedAccountId ||
                (!selectedTemplate && !body.trim())
              }
              className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-3 py-2 text-[11px] font-medium disabled:opacity-50"
            >
              {previewPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <MessageCircle className="h-3 w-3" />
              )}
              {previewPending ? copy.previewing : copy.preview}
            </button>
            <button
              type="button"
              onClick={sendMessage}
              disabled={
                sendPending ||
                !resolvedContactId ||
                !resolvedAccountId ||
                (!selectedTemplate && !body.trim())
              }
              className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-2 text-[11px] font-medium text-bg disabled:opacity-50"
            >
              {sendPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : scheduledFor ? (
                <Clock3 className="h-3 w-3" />
              ) : (
                <Send className="h-3 w-3" />
              )}
              {sendPending
                ? copy.sending
                : scheduledFor
                  ? isSandboxAccount(selectedAccount)
                    ? copy.scheduleSandbox
                    : copy.schedule
                  : isSandboxAccount(selectedAccount)
                    ? copy.recordSandbox
                    : copy.sendNow}
            </button>
          </div>
          </section>
        ) : null}

        {canReadHistory ? (
        <section>
          <div className="mb-2 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {copy.threadHistory}
          </div>
          {threads.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border px-3 py-5 text-center text-[11px] text-text-faint">
              {copy.noMessages}
            </p>
          ) : (
            <div className="grid gap-3 lg:grid-cols-[190px_1fr]">
              <div className="space-y-1">
                {threads.map((thread) => (
                  <button
                    key={thread.id}
                    type="button"
                    onClick={() => void openThread(thread.id)}
                    className={cn(
                      "w-full rounded-[6px] border px-2.5 py-2 text-left",
                      selectedThreadId === thread.id
                        ? "border-border-strong bg-bg-active"
                        : "border-border bg-bg-elevated hover:bg-bg-hover",
                    )}
                  >
                    <span className="block text-[10.5px] font-medium uppercase">
                      {thread.channel}
                    </span>
                    <span className="mt-0.5 block truncate text-[10px] text-text-faint">
                      {thread.latest_message?.body ?? copy.noMessages}
                    </span>
                  </button>
                ))}
              </div>
              <div className="min-h-24 rounded-lg border border-border bg-bg-elevated p-3">
                {historyPending && messages.length === 0 ? (
                  <div className="flex justify-center py-6">
                    <Loader2 className="h-4 w-4 animate-spin text-text-faint" />
                  </div>
                ) : messages.length === 0 ? (
                  <button
                    type="button"
                    onClick={() =>
                      selectedThreadId && void openThread(selectedThreadId)
                    }
                    className="w-full py-6 text-[11px] text-text-faint"
                  >
                    {copy.noMessages}
                  </button>
                ) : (
                  <div className="space-y-2">
                    {messages.map((message) => (
                      <article
                        key={message.id}
                        className={cn(
                          "max-w-[86%] rounded-lg border px-3 py-2",
                          message.direction === "outbound"
                            ? "ml-auto border-primary/20 bg-primary/5"
                            : "border-border bg-bg",
                        )}
                      >
                        <div className="mb-1 flex items-center justify-between gap-3 text-[9.5px] text-text-faint">
                          <span>
                            {message.direction === "outbound"
                              ? copy.outbound
                              : copy.inbound}
                          </span>
                          <span>
                            {message.direction === "outbound" &&
                            message.account_id &&
                            accountProviders.get(message.account_id) === "sandbox"
                              ? copy.sandboxStatus
                              : copy.statuses[message.status] ?? message.status} ·{" "}
                            {new Intl.DateTimeFormat(
                              locale === "ru" ? "ru-RU" : "en-US",
                              { dateStyle: "short", timeStyle: "short" },
                            ).format(new Date(message.created_at))}
                          </span>
                        </div>
                        {message.subject ? (
                          <p className="text-[10.5px] font-medium">
                            {message.subject}
                          </p>
                        ) : null}
                        <p className="whitespace-pre-wrap text-[11px] leading-5 text-text-muted">
                          {message.body}
                        </p>
                        {message.status === "queued" ? (
                          <button
                            type="button"
                            onClick={() => cancelMessage(message)}
                            className="mt-1 text-[9.5px] text-score-low hover:underline"
                          >
                            {copy.cancelMessage}
                          </button>
                        ) : null}
                      </article>
                    ))}
                    {messagesCursor ? (
                      <button
                        type="button"
                        disabled={historyPending}
                        onClick={() =>
                          void openThread(selectedThreadId, messagesCursor)
                        }
                        className="w-full py-1 text-[10.5px] text-text-muted hover:text-text"
                      >
                        {copy.loadMore}
                      </button>
                    ) : null}
                  </div>
                )}
              </div>
            </div>
          )}
        </section>
        ) : null}
      </div>
    </Collapsible>
  );
}

function Field({
  label,
  className,
  children,
}: {
  label: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <label className={cn("grid gap-1", className)}>
      <span className="text-[9.5px] font-medium uppercase tracking-[0.06em] text-text-faint">
        {label}
      </span>
      {children}
    </label>
  );
}

function uniqueMessages(messages: CommunicationMessage[]): CommunicationMessage[] {
  const seen = new Set<string>();
  return messages.filter((message) => {
    if (seen.has(message.id)) return false;
    seen.add(message.id);
    return true;
  });
}
