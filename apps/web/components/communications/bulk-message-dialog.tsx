"use client";

import { useEffect, useMemo, useRef, useState, useTransition } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Mail,
  RefreshCw,
  Send,
  Users,
} from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  createBulkCommunicationAction,
  listCommunicationAccountsAction,
  listCommunicationTemplatesAction,
  previewBulkCommunicationAction,
} from "@/lib/actions";
import { candDisplayRef } from "@/lib/format";
import type {
  BulkCommunicationPreview,
  CandidateSearchItem,
  CommunicationAccount,
  CommunicationTemplate,
  OutboundMessageChannel,
  Position,
} from "@/lib/types";
import {
  isCommunicationAccountAvailable,
  isSandboxAccount,
} from "@/lib/communication-capabilities";
import { cn } from "@/lib/utils";

const controlClass =
  "w-full rounded-[6px] border border-border bg-bg px-2.5 py-2 text-[11px] text-text outline-none transition-colors focus:border-border-strong";

function newIdempotencyKey(): string {
  const id =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `bulk-message-${id}`;
}

export function BulkMessageDialog({
  open,
  onOpenChange,
  candidates,
  positions,
  synchronousRecipientLimit,
  durableRecipientLimit,
  onSent,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  candidates: CandidateSearchItem[];
  positions: Position[];
  synchronousRecipientLimit: number | null;
  durableRecipientLimit: number | null;
  onSent: () => void;
}) {
  const { m } = useI18n();
  const copy = m.communications;
  const [accounts, setAccounts] = useState<CommunicationAccount[]>([]);
  const [templates, setTemplates] = useState<CommunicationTemplate[]>([]);
  const [resourcesFailed, setResourcesFailed] = useState(false);
  const [channel, setChannel] = useState<OutboundMessageChannel>("email");
  const [accountId, setAccountId] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [positionId, setPositionId] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [scheduledFor, setScheduledFor] = useState("");
  const [preview, setPreview] = useState<BulkCommunicationPreview | null>(null);
  const [previewSignature, setPreviewSignature] = useState<string | null>(null);
  const [resourcesPending, startResources] = useTransition();
  const [previewPending, startPreview] = useTransition();
  const [sendPending, startSend] = useTransition();
  const idempotencyKey = useRef<string | null>(null);
  const previousSignature = useRef("");
  const candidateKey = useMemo(
    () => candidates.map((candidate) => candidate.id).sort().join(":"),
    [candidates],
  );
  const recipientLimitExceeded =
    durableRecipientLimit == null || candidates.length > durableRecipientLimit;

  const channelAccounts = useMemo(
    () =>
      accounts.filter(
        (account) => account.channel === channel && account.is_active,
      ),
    [accounts, channel],
  );
  const channelTemplates = useMemo(
    () =>
      templates.filter(
        (template) => template.channel === channel && template.is_active,
      ),
    [channel, templates],
  );
  const availableChannelAccounts = useMemo(
    () => channelAccounts.filter(isCommunicationAccountAvailable),
    [channelAccounts],
  );
  const resolvedAccountId =
    availableChannelAccounts.find((account) => account.id === accountId)?.id ??
    availableChannelAccounts[0]?.id ??
    "";
  const selectedAccount = channelAccounts.find(
    (account) => account.id === resolvedAccountId,
  );
  const selectedTemplate = channelTemplates.find(
    (template) => template.id === templateId,
  );

  const formSignature = JSON.stringify({
    candidates: candidateKey,
    channel,
    accountId: resolvedAccountId,
    templateId: selectedTemplate?.id ?? null,
    positionId: positionId || null,
    subject: selectedTemplate || channel !== "email" ? null : subject.trim(),
    body: selectedTemplate ? null : body.trim(),
    scheduledFor,
  });
  const currentPreview =
    previewSignature === formSignature ? preview : null;

  useEffect(() => {
    if (previousSignature.current !== formSignature) {
      previousSignature.current = formSignature;
      idempotencyKey.current = null;
    }
  }, [formSignature]);

  function loadResources() {
    setPreview(null);
    setPreviewSignature(null);
    setResourcesFailed(false);
    startResources(async () => {
      const [accountResult, templateResult] = await Promise.all([
        listCommunicationAccountsAction(),
        listCommunicationTemplatesAction(),
      ]);
      if (!accountResult.ok || !templateResult.ok) {
        setResourcesFailed(true);
        return;
      }
      setAccounts(accountResult.data);
      setTemplates(templateResult.data);
    });
  }

  useEffect(() => {
    if (!open) return;
    loadResources();
    // The selected candidate set is intentionally a reload boundary.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidateKey, open]);

  function composeBody() {
    return {
      candidate_ids: candidates.map((candidate) => candidate.id),
      channel,
      account_id: resolvedAccountId || null,
      template_id: selectedTemplate?.id ?? null,
      position_id: positionId || null,
      subject:
        selectedTemplate || channel !== "email" ? null : subject.trim() || null,
      body: selectedTemplate ? null : body.trim() || null,
    };
  }

  function prepare() {
    if (
      recipientLimitExceeded ||
      previewPending ||
      !resolvedAccountId ||
      (!selectedTemplate && !body.trim())
    ) {
      return;
    }
    const signature = formSignature;
    startPreview(async () => {
      const result = await previewBulkCommunicationAction(composeBody());
      if (!result.ok) {
        setPreview(null);
        setPreviewSignature(null);
        toast.error(`${copy.previewFailed}: ${result.error}`);
        return;
      }
      setPreview(result.data);
      setPreviewSignature(signature);
    });
  }

  function sendBatch() {
    if (
      recipientLimitExceeded ||
      !currentPreview ||
      (currentPreview.complete && currentPreview.ready_count === 0) ||
      sendPending
    )
      return;
    idempotencyKey.current ??= newIdempotencyKey();
    const key = idempotencyKey.current;
    startSend(async () => {
      const result = await createBulkCommunicationAction({
        ...composeBody(),
        scheduled_for: scheduledFor
          ? new Date(scheduledFor).toISOString()
          : null,
        idempotency_key: key,
      });
      if (!result.ok) {
        toast.error(`${copy.sendFailed}: ${result.error}`);
        return;
      }
      toast.success(
        isSandboxAccount(selectedAccount)
          ? copy.bulkRecordedSandbox(
              result.data.queued_count,
              result.data.skipped.length,
            )
          : copy.bulkSent(
              result.data.queued_count,
              result.data.skipped.length,
            ),
      );
      idempotencyKey.current = null;
      onSent();
      onOpenChange(false);
    });
  }

  const labelById = new Map(
    candidates.map((candidate) => [
      candidate.id,
      candDisplayRef(candidate.id, candidate.source_ref),
    ]),
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[720px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-[15px]">
            <Mail className="h-4 w-4 text-text-muted" /> {copy.bulkTitle}
          </DialogTitle>
          <DialogDescription className="text-[12px] text-text-muted">
            {copy.bulkDesc(candidates.length)}{" "}
            {synchronousRecipientLimit == null || durableRecipientLimit == null
              ? copy.loadFailed
              : `${copy.bulkLimit(synchronousRecipientLimit)} ${copy.bulkDurableLimit(durableRecipientLimit)}`}
          </DialogDescription>
        </DialogHeader>

        {recipientLimitExceeded ? (
          <p
            role="alert"
            className="rounded-lg border border-score-low/25 bg-score-low/5 px-3 py-2 text-[11px] text-score-low"
          >
            {durableRecipientLimit == null
              ? copy.loadFailed
              : copy.bulkLimitExceeded(candidates.length, durableRecipientLimit)}
          </p>
        ) : null}

        {resourcesPending ? (
          <div className="flex items-center justify-center gap-2 py-12 text-[12px] text-text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> {copy.loadingComposer}
          </div>
        ) : resourcesFailed ? (
          <div className="flex flex-col items-center gap-3 rounded-lg border border-score-low/25 bg-score-low/5 px-4 py-9 text-center">
            <p className="text-[12px] text-score-low">{copy.loadFailed}</p>
            <button
              type="button"
              onClick={loadResources}
              className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-3 py-1.5 text-[11px]"
            >
              <RefreshCw className="h-3.5 w-3.5" /> {copy.retry}
            </button>
          </div>
        ) : (
          <div className="max-h-[66vh] space-y-4 overflow-y-auto pr-1">
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              <Field label={copy.channel}>
                <select
                  value={channel}
                  onChange={(event) => {
                    setChannel(event.target.value as OutboundMessageChannel);
                    setAccountId("");
                    setTemplateId("");
                    setSubject("");
                  }}
                  className={controlClass}
                >
                  <option value="email">Email</option>
                  <option value="telegram">Telegram</option>
                </select>
              </Field>
              <Field label={copy.account}>
                <select
                  value={resolvedAccountId}
                  onChange={(event) => setAccountId(event.target.value)}
                  className={controlClass}
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
                  onChange={(event) => setTemplateId(event.target.value)}
                  className={controlClass}
                >
                  <option value="">{copy.customMessage}</option>
                  {channelTemplates.map((template) => (
                    <option key={template.id} value={template.id}>
                      {template.name} · {template.language.toUpperCase()}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={copy.bulkPosition}>
                <select
                  value={positionId}
                  onChange={(event) => setPositionId(event.target.value)}
                  className={controlClass}
                >
                  <option value="">{copy.noPosition}</option>
                  {positions
                    .filter((position) => position.status !== "closed")
                    .map((position) => (
                      <option key={position.id} value={position.id}>
                        {position.title}
                      </option>
                    ))}
                </select>
              </Field>
            </div>

            {channel === "email" ? (
              <Field label={copy.subject}>
                <input
                  value={selectedTemplate?.subject_template ?? subject}
                  onChange={(event) => setSubject(event.target.value)}
                  disabled={Boolean(selectedTemplate)}
                  maxLength={500}
                  className={cn(controlClass, "disabled:opacity-60")}
                />
              </Field>
            ) : null}
            <Field label={copy.body}>
              <textarea
                value={selectedTemplate?.body_template ?? body}
                onChange={(event) => setBody(event.target.value)}
                disabled={Boolean(selectedTemplate)}
                rows={6}
                maxLength={20_000}
                className={cn(controlClass, "resize-y disabled:opacity-60")}
              />
            </Field>
            <div className="grid gap-2 sm:grid-cols-[240px_1fr] sm:items-end">
              <Field label={copy.scheduledFor}>
                <input
                  type="datetime-local"
                  value={scheduledFor}
                  onChange={(event) => setScheduledFor(event.target.value)}
                  className={controlClass}
                />
              </Field>
              <p className="pb-2 text-[10.5px] text-text-faint">
                {copy.bulkSafety}
              </p>
            </div>

            {!resolvedAccountId ? (
              <p className="rounded-lg border border-score-mid/25 bg-score-mid/5 px-3 py-2 text-[10.5px] text-score-mid">
                {copy.noAccount}
              </p>
            ) : null}
            {isSandboxAccount(selectedAccount) ? (
              <p className="rounded-lg border border-score-mid/25 bg-score-mid/5 px-3 py-2 text-[10.5px] text-score-mid">
                {copy.sandboxNotice}
              </p>
            ) : null}

            {currentPreview ? (
              <div className="rounded-lg border border-border bg-bg-elevated p-3">
                {!currentPreview.complete ? (
                  <p className="mb-3 rounded-[6px] border border-score-mid/25 bg-score-mid/5 px-2.5 py-2 text-[10.5px] text-score-mid">
                    {copy.bulkPreviewOmitted(
                      currentPreview.requested_count,
                      currentPreview.synchronous_recipient_limit,
                    )}
                  </p>
                ) : null}
                <div className="flex flex-wrap items-center gap-2">
                  {currentPreview.complete ? (
                    <>
                      <span className="inline-flex items-center gap-1.5 rounded-full bg-score-high/10 px-2 py-1 text-[10.5px] text-score-high">
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        {copy.ready(currentPreview.ready_count)}
                      </span>
                      <span className="inline-flex items-center gap-1.5 rounded-full bg-score-mid/10 px-2 py-1 text-[10.5px] text-score-mid">
                        <AlertTriangle className="h-3.5 w-3.5" />
                        {copy.skipped(currentPreview.skipped_count)}
                      </span>
                    </>
                  ) : null}
                </div>
                {currentPreview.complete ? <div className="mt-3 max-h-56 space-y-1.5 overflow-y-auto">
                  {currentPreview.items.map((item) => (
                    <div
                      key={item.candidate_id}
                      className="flex items-start justify-between gap-3 rounded-[6px] border border-border bg-bg px-2.5 py-2"
                    >
                      <div className="min-w-0">
                        <p className="truncate font-mono text-[10.5px] text-text">
                          {labelById.get(item.candidate_id) ??
                            item.candidate_id.slice(0, 8)}
                        </p>
                        {item.preview ? (
                          <p className="mt-0.5 truncate text-[10px] text-text-faint">
                            {item.preview.target.masked_value} · {item.preview.body}
                          </p>
                        ) : null}
                      </div>
                      <span
                        className={cn(
                          "shrink-0 rounded-full px-2 py-0.5 text-[9.5px]",
                          item.status === "ready"
                            ? "bg-score-high/10 text-score-high"
                            : "bg-score-mid/10 text-score-mid",
                        )}
                      >
                        {item.status === "ready"
                          ? copy.recipientReady
                          : item.status === "opted_out"
                            ? copy.recipientOptedOut
                            : copy.recipientMissingContact}
                      </span>
                    </div>
                  ))}
                </div> : null}
              </div>
            ) : null}
          </div>
        )}

        <DialogFooter className="mt-2 flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="rounded-[6px] px-3 py-2 text-[11px] text-text-muted hover:bg-bg-hover"
          >
            {m.common.cancel}
          </button>
          <button
            type="button"
            onClick={prepare}
            disabled={
              resourcesPending ||
              resourcesFailed ||
              recipientLimitExceeded ||
              previewPending ||
              !resolvedAccountId ||
              (!selectedTemplate && !body.trim())
            }
            className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-3 py-2 text-[11px] font-medium disabled:opacity-50"
          >
            {previewPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Users className="h-3.5 w-3.5" />
            )}
            {copy.bulkReview}
          </button>
          <button
            type="button"
            onClick={sendBatch}
            disabled={
              !currentPreview ||
              (currentPreview.complete && currentPreview.ready_count === 0) ||
              recipientLimitExceeded ||
              sendPending
            }
            className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-2 text-[11px] font-medium text-bg disabled:opacity-50"
          >
            {sendPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Send className="h-3.5 w-3.5" />
            )}
            {sendPending
              ? copy.sending
              : isSandboxAccount(selectedAccount)
                ? copy.bulkRecordSandbox
                : copy.bulkSend}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-[10px] font-medium uppercase tracking-[0.06em] text-text-faint">
      {label}
      <div className="mt-1.5">{children}</div>
    </label>
  );
}
