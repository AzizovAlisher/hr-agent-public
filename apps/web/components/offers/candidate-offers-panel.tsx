"use client";

import {
  BriefcaseBusiness,
  Check,
  Clipboard,
  ExternalLink,
  FileSignature,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Send,
  ShieldCheck,
  Undo2,
  X,
} from "lucide-react";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  createCandidateOfferAction,
  listCandidateHandoffsAction,
  listCandidateOffersAction,
  rotateCandidateOfferLinkAction,
  sendCandidateOfferAction,
  submitCandidateOfferAction,
  updateCandidateHandoffAction,
  updateCandidateOfferAction,
  voteCandidateOfferAction,
  withdrawCandidateOfferAction,
} from "@/lib/actions";
import type {
  CandidateHandoff,
  CandidateOffer,
  HandoffChecklistItem,
  OfferTemplate,
} from "@/lib/types";

const fieldClass =
  "block min-h-9 w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[12px] text-text outline-none placeholder:text-text-faint focus:border-border-strong";

/** @ui-only Candidate-offer editor state. */
type OfferForm = {
  positionId: string;
  templateId: string;
  subject: string;
  body: string;
  salary: string;
  currency: string;
  startDate: string;
  expiresAt: string;
  approvals: string;
};

function blankForm(positionId = "", templateId = ""): OfferForm {
  return {
    positionId,
    templateId,
    subject: "",
    body: "",
    salary: "",
    currency: "TJS",
    startDate: "",
    expiresAt: "",
    approvals: "1",
  };
}

function localDateTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function formatMoney(value: string | number | null, currency: string | null) {
  if (value == null) return null;
  return `${value} ${currency ?? ""}`.trim();
}

export function CandidateOffersPanel({
  candidateId,
  positions,
  templates,
  initialOffers,
  initialHandoffs,
  canRead,
  canManage,
  canViewCompensation,
  initialOffersFailed,
  initialHandoffsFailed,
}: {
  candidateId: string;
  positions: { id: string; title: string }[];
  templates: OfferTemplate[];
  initialOffers: CandidateOffer[];
  initialHandoffs: CandidateHandoff[];
  canRead: boolean;
  canManage: boolean;
  canViewCompensation: boolean;
  initialOffersFailed: boolean;
  initialHandoffsFailed: boolean;
}) {
  const { m } = useI18n();
  const copy = m.offers;
  const [offers, setOffers] = useState(initialOffers);
  const [handoffs, setHandoffs] = useState(initialHandoffs);
  const [offersFailed, setOffersFailed] = useState(initialOffersFailed);
  const [handoffsFailed, setHandoffsFailed] = useState(initialHandoffsFailed);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<OfferForm>(
    blankForm(positions[0]?.id, templates[0]?.id),
  );
  const [voteComments, setVoteComments] = useState<Record<string, string>>({});
  const [oneTimeLinks, setOneTimeLinks] = useState<Record<string, string>>({});
  const [pending, startTransition] = useTransition();

  const usingTemplate = Boolean(form.templateId) && !editingId;
  const salary =
    !canViewCompensation || form.salary.trim() === ""
      ? null
      : Number(form.salary);
  const approvals = Number(form.approvals);
  const valid =
    Boolean(form.positionId) &&
    (usingTemplate || (form.subject.trim() && form.body.trim())) &&
    (salary == null || (Number.isFinite(salary) && salary >= 0)) &&
    (!canViewCompensation || /^[A-Z]{3}$/.test(form.currency)) &&
    Number.isInteger(approvals) &&
    approvals >= 1 &&
    approvals <= 20;

  function replaceOffer(next: CandidateOffer) {
    setOffers((current) => {
      const found = current.some((item) => item.id === next.id);
      return found
        ? current.map((item) => (item.id === next.id ? next : item))
        : [next, ...current];
    });
  }

  function closeForm() {
    setFormOpen(false);
    setEditingId(null);
    setForm(blankForm(positions[0]?.id, templates[0]?.id));
  }

  function editOffer(offer: CandidateOffer) {
    setEditingId(offer.id);
    setForm({
      positionId: offer.position_id ?? positions[0]?.id ?? "",
      templateId: "",
      subject: offer.subject_source ?? offer.subject ?? "",
      body: offer.body_source ?? offer.body ?? "",
      salary: offer.salary_amount == null ? "" : String(offer.salary_amount),
      currency: offer.salary_currency ?? "TJS",
      startDate: offer.start_date ?? "",
      expiresAt: localDateTime(offer.expires_at),
      approvals: String(offer.required_approvals),
    });
    setFormOpen(true);
  }

  function saveOffer(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || pending || !canManage) return;
    const expiresAt = form.expiresAt
      ? new Date(form.expiresAt).toISOString()
      : null;
    startTransition(async () => {
      const result = editingId
        ? await updateCandidateOfferAction(candidateId, editingId, {
            subject: form.subject.trim(),
            body: form.body.trim(),
            ...(canViewCompensation
              ? { salary_amount: salary, salary_currency: form.currency }
              : {}),
            start_date: form.startDate || null,
            expires_at: expiresAt,
            required_approvals: approvals,
          })
        : await createCandidateOfferAction(candidateId, {
            position_id: form.positionId,
            template_id: form.templateId || null,
            subject: form.templateId ? null : form.subject.trim(),
            body: form.templateId ? null : form.body.trim(),
            ...(canViewCompensation ? { salary_amount: salary } : {}),
            salary_currency: form.currency,
            start_date: form.startDate || null,
            expires_at: expiresAt,
            required_approvals: approvals,
          });
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      replaceOffer(result.data);
      toast.success(editingId ? copy.offerUpdated : copy.offerCreated);
      closeForm();
    });
  }

  function mutate(
    action: () => Promise<
      | { ok: true; data: CandidateOffer }
      | { ok: false; error: string }
    >,
    success: string,
  ) {
    startTransition(async () => {
      const result = await action();
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      replaceOffer(result.data);
      toast.success(success);
    });
  }

  function refresh() {
    startTransition(async () => {
      const [offersResult, handoffsResult] = await Promise.all([
        listCandidateOffersAction(candidateId),
        listCandidateHandoffsAction(candidateId),
      ]);
      setOffersFailed(!offersResult.ok);
      setHandoffsFailed(!handoffsResult.ok);
      if (offersResult.ok) setOffers(offersResult.data);
      if (handoffsResult.ok) setHandoffs(handoffsResult.data);
    });
  }

  function sendOffer(offerId: string) {
    startTransition(async () => {
      const result = await sendCandidateOfferAction(candidateId, offerId);
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      replaceOffer(result.data.offer);
      setOneTimeLinks((current) => ({ ...current, [offerId]: result.data.url }));
      toast.success(copy.sent);
    });
  }

  function rotateOfferLink(offerId: string) {
    startTransition(async () => {
      const result = await rotateCandidateOfferLinkAction(candidateId, offerId);
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      replaceOffer(result.data.offer);
      setOneTimeLinks((current) => ({ ...current, [offerId]: result.data.url }));
      toast.success(copy.rotated);
    });
  }

  function updateHandoff(
    handoff: CandidateHandoff,
    item: HandoffChecklistItem,
    checked: boolean,
  ) {
    startTransition(async () => {
      const result = await updateCandidateHandoffAction(
        candidateId,
        handoff.id,
        { [item]: checked },
      );
      if (!result.ok) {
        toast.error(copy.checklistSaveFailed);
        return;
      }
      setHandoffs((current) =>
        current.map((row) => (row.id === result.data.id ? result.data : row)),
      );
      toast.success(copy.checklistSaved);
    });
  }

  return (
    <section className="rounded-lg border border-border bg-bg-elevated">
      <header className="flex flex-col gap-3 border-b border-border px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="flex items-center gap-2 text-[13px] font-semibold">
            <FileSignature className="h-4 w-4 text-text-muted" />
            {copy.title}
          </h2>
          <p className="mt-1 text-[11px] text-text-faint">{copy.subtitle}</p>
        </div>
        <div className="flex items-center gap-2">
          {(offersFailed || handoffsFailed) ? (
            <button
              type="button"
              disabled={pending}
              onClick={refresh}
              className="inline-flex items-center gap-1.5 rounded-[6px] border border-border px-2.5 py-1.5 text-[11px] text-text-muted"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${pending ? "animate-spin" : ""}`} />
              {copy.retry}
            </button>
          ) : null}
          {canManage && positions.length > 0 ? (
            <button
              type="button"
              onClick={() => {
                setEditingId(null);
                setForm(blankForm(positions[0]?.id, templates[0]?.id));
                setFormOpen((value) => !value);
              }}
              className="inline-flex items-center gap-1.5 rounded-[6px] bg-primary px-2.5 py-1.5 text-[11px] font-medium text-primary-foreground"
            >
              <Plus className="h-3.5 w-3.5" />
              {copy.newOffer}
            </button>
          ) : null}
        </div>
      </header>

      {!canRead ? (
        <div className="flex items-start gap-2 px-4 py-4 text-[11.5px] text-text-muted">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />
          {copy.accessRestricted}
        </div>
      ) : (
        <div className="space-y-5 p-4">
          {offersFailed ? (
            <p role="alert" className="rounded-md border border-red-500/20 bg-red-500/5 px-3 py-2 text-[11.5px] text-red-500">
              {copy.loadFailed}
            </p>
          ) : null}

          {formOpen ? (
            <OfferEditor
              copy={copy}
              form={form}
              setForm={setForm}
              positions={positions}
              templates={templates}
              editing={Boolean(editingId)}
              usingTemplate={usingTemplate}
              valid={Boolean(valid)}
              pending={pending}
              canViewCompensation={canViewCompensation}
              cancelLabel={m.common.cancel}
              onSubmit={saveOffer}
              onCancel={closeForm}
            />
          ) : null}

          {!offersFailed && offers.length === 0 ? (
            <div className="rounded-md border border-dashed border-border px-4 py-8 text-center text-[11.5px] text-text-faint">
              {copy.empty}
            </div>
          ) : (
            <div className="space-y-3">
              {offers.map((offer) => (
                <OfferCard
                  key={offer.id}
                  offer={offer}
                  copy={copy}
                  pending={pending}
                  comment={voteComments[offer.id] ?? ""}
                  oneTimeLink={oneTimeLinks[offer.id] ?? null}
                  canManage={canManage}
                  onComment={(value) =>
                    setVoteComments((current) => ({ ...current, [offer.id]: value }))
                  }
                  onEdit={() => editOffer(offer)}
                  onSubmit={() => {
                    if (!window.confirm(copy.submitConfirm)) return;
                    mutate(
                      () => submitCandidateOfferAction(candidateId, offer.id),
                      copy.offerUpdated,
                    );
                  }}
                  onVote={(vote) =>
                    mutate(
                      () =>
                        voteCandidateOfferAction(candidateId, offer.id, {
                          vote,
                          comment: voteComments[offer.id]?.trim() || null,
                        }),
                      copy.offerUpdated,
                    )
                  }
                  onSend={() => {
                    if (window.confirm(copy.sendConfirm)) sendOffer(offer.id);
                  }}
                  onRotate={() => {
                    if (window.confirm(copy.rotateConfirm)) rotateOfferLink(offer.id);
                  }}
                  onWithdraw={() => {
                    if (!window.confirm(copy.withdrawConfirm)) return;
                    mutate(
                      () => withdrawCandidateOfferAction(candidateId, offer.id),
                      copy.offerUpdated,
                    );
                  }}
                />
              ))}
            </div>
          )}

          <Handoffs
            copy={copy}
            rows={handoffs}
            failed={handoffsFailed}
            pending={pending}
            canManage={canManage}
            onChange={updateHandoff}
          />
        </div>
      )}
    </section>
  );
}

function OfferEditor({
  copy,
  form,
  setForm,
  positions,
  templates,
  editing,
  usingTemplate,
  valid,
  pending,
  canViewCompensation,
  cancelLabel,
  onSubmit,
  onCancel,
}: {
  copy: ReturnType<typeof useI18n>["m"]["offers"];
  form: OfferForm;
  setForm: React.Dispatch<React.SetStateAction<OfferForm>>;
  positions: { id: string; title: string }[];
  templates: OfferTemplate[];
  editing: boolean;
  usingTemplate: boolean;
  valid: boolean;
  pending: boolean;
  canViewCompensation: boolean;
  cancelLabel: string;
  onSubmit: (event: React.FormEvent) => void;
  onCancel: () => void;
}) {
  const set = (key: keyof OfferForm, value: string) =>
    setForm((current) => ({ ...current, [key]: value }));
  return (
    <form onSubmit={onSubmit} className="space-y-3 rounded-lg border border-border bg-bg p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-[12.5px] font-semibold">{editing ? copy.editTitle : copy.createTitle}</h3>
        <button type="button" onClick={onCancel} aria-label={cancelLabel}><X className="h-4 w-4 text-text-muted" /></button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={copy.position}>
          <select value={form.positionId} disabled={editing} onChange={(event) => set("positionId", event.target.value)} className={fieldClass}>
            {positions.map((position) => <option key={position.id} value={position.id}>{position.title}</option>)}
          </select>
        </Field>
        {!editing ? (
          <Field label={copy.offerTemplate}>
            <select value={form.templateId} onChange={(event) => set("templateId", event.target.value)} className={fieldClass}>
              <option value="">{copy.manualOffer}</option>
              {templates.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}
            </select>
          </Field>
        ) : null}
      </div>
      {!usingTemplate ? (
        <>
          <Field label={copy.subject}><input value={form.subject} onChange={(event) => set("subject", event.target.value)} required maxLength={500} className={fieldClass} /></Field>
          <Field label={copy.body}><textarea value={form.body} onChange={(event) => set("body", event.target.value)} required maxLength={30_000} rows={7} className={fieldClass} /></Field>
        </>
      ) : null}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {canViewCompensation ? (
          <>
            <Field label={copy.salaryAmount}><input value={form.salary} onChange={(event) => set("salary", event.target.value)} type="number" min="0" step="0.01" className={fieldClass} /></Field>
            <Field label={copy.salaryCurrency}><input value={form.currency} onChange={(event) => set("currency", event.target.value.toUpperCase().slice(0, 3))} maxLength={3} className={fieldClass} /></Field>
          </>
        ) : null}
        <Field label={copy.startDate}><input value={form.startDate} onChange={(event) => set("startDate", event.target.value)} type="date" className={fieldClass} /></Field>
        <Field label={copy.expiresAt}><input value={form.expiresAt} onChange={(event) => set("expiresAt", event.target.value)} type="datetime-local" className={fieldClass} /></Field>
      </div>
      <Field label={copy.requiredApprovals}><input value={form.approvals} onChange={(event) => set("approvals", event.target.value)} type="number" min="1" max="20" className={`${fieldClass} max-w-32`} /></Field>
      <div className="flex gap-2">
        <button disabled={!valid || pending} className="inline-flex items-center gap-1.5 rounded-[6px] bg-primary px-3 py-2 text-[11.5px] font-medium text-primary-foreground disabled:opacity-50">
          {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
          {editing ? copy.offerUpdated : copy.createOffer}
        </button>
        <button type="button" onClick={onCancel} className="rounded-[6px] border border-border px-3 py-2 text-[11.5px] text-text-muted">{cancelLabel}</button>
      </div>
    </form>
  );
}

function OfferCard({
  offer,
  copy,
  pending,
  canManage,
  comment,
  oneTimeLink,
  onComment,
  onEdit,
  onSubmit,
  onVote,
  onSend,
  onRotate,
  onWithdraw,
}: {
  offer: CandidateOffer;
  copy: ReturnType<typeof useI18n>["m"]["offers"];
  pending: boolean;
  canManage: boolean;
  comment: string;
  oneTimeLink: string | null;
  onComment: (value: string) => void;
  onEdit: () => void;
  onSubmit: () => void;
  onVote: (vote: "approve" | "reject") => void;
  onSend: () => void;
  onRotate: () => void;
  onWithdraw: () => void;
}) {
  const approvals = offer.approvals.filter((item) => item.vote === "approve").length;
  const canWithdraw = ["draft", "pending", "approved", "sent"].includes(offer.status);
  return (
    <article className="rounded-lg border border-border bg-bg px-4 py-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-[13px] font-semibold">{offer.position_title}</h3>
            <span className="rounded-full border border-border px-2 py-0.5 text-[9.5px] font-medium text-text-muted">{copy.statuses[offer.status] ?? offer.status}</span>
          </div>
          <p className="mt-1 text-[10.5px] text-text-faint">{copy.approvals(approvals, offer.required_approvals)}</p>
          {offer.compensation_visible && offer.salary_amount != null ? (
            <p className="mt-1 text-[11.5px] font-medium">{formatMoney(offer.salary_amount, offer.salary_currency)}</p>
          ) : null}
        </div>
        {canManage ? <div className="flex flex-wrap gap-1.5">
          {offer.status === "draft" ? <Action icon={<Pencil />} label={copy.edit} disabled={pending} onClick={onEdit} /> : null}
          {offer.status === "draft" ? <Action icon={<Send />} label={copy.submit} disabled={pending} onClick={onSubmit} /> : null}
          {offer.status === "approved" ? <Action icon={<ExternalLink />} label={copy.send} disabled={pending} onClick={onSend} /> : null}
          {offer.status === "sent" || offer.status === "expired" ? <Action icon={<RefreshCw />} label={copy.rotateLink} disabled={pending} onClick={onRotate} /> : null}
          {canWithdraw ? <Action icon={<Undo2 />} label={copy.withdraw} disabled={pending} onClick={onWithdraw} /> : null}
        </div> : null}
      </div>

      {offer.subject && offer.body ? (
        <details className="mt-3 rounded-md border border-border bg-bg-elevated">
          <summary className="cursor-pointer px-3 py-2 text-[11px] font-medium text-text-muted">{copy.renderedPreview}</summary>
          <div className="border-t border-border px-3 py-3 text-[11.5px] leading-5 text-text-muted">
            <strong className="block text-text">{offer.subject}</strong>
            <p className="mt-2 whitespace-pre-wrap">{offer.body}</p>
          </div>
        </details>
      ) : null}

      {canManage && offer.status === "pending" ? (
        <div className="mt-3 flex flex-col gap-2 border-t border-border pt-3 sm:flex-row">
          <input value={comment} onChange={(event) => onComment(event.target.value)} maxLength={4000} placeholder={copy.voteCommentPlaceholder} className={`${fieldClass} flex-1`} />
          <Action icon={<Check />} label={copy.approve} disabled={pending} onClick={() => onVote("approve")} />
          <Action icon={<X />} label={copy.reject} disabled={pending} onClick={() => onVote("reject")} />
        </div>
      ) : null}

      {oneTimeLink ? (
        <div className="mt-3 rounded-md border border-emerald-500/25 bg-emerald-500/5 p-3">
          <div className="text-[10.5px] font-medium text-emerald-600">{copy.publicLinkTitle}</div>
          <div className="mt-2 flex gap-2">
            <input readOnly value={oneTimeLink} className={`${fieldClass} min-w-0 flex-1 font-mono text-[10px]`} />
            <button type="button" onClick={() => navigator.clipboard.writeText(oneTimeLink).then(() => toast.success(copy.copied)).catch(() => toast.error(copy.copyFailed))} className="inline-flex items-center gap-1 rounded-[6px] border border-border px-2.5 text-[11px]"><Clipboard className="h-3.5 w-3.5" />{copy.copyLink}</button>
          </div>
          <p className="mt-1.5 text-[10px] text-text-faint">{copy.linkOnce}</p>
        </div>
      ) : null}
    </article>
  );
}

function Handoffs({
  copy,
  rows,
  failed,
  pending,
  canManage,
  onChange,
}: {
  copy: ReturnType<typeof useI18n>["m"]["offers"];
  rows: CandidateHandoff[];
  failed: boolean;
  pending: boolean;
  canManage: boolean;
  onChange: (row: CandidateHandoff, item: HandoffChecklistItem, checked: boolean) => void;
}) {
  const items: HandoffChecklistItem[] = ["documents", "equipment", "accounts", "first_day"];
  return (
    <section className="border-t border-border pt-5">
      <h3 className="flex items-center gap-2 text-[12.5px] font-semibold"><BriefcaseBusiness className="h-4 w-4 text-text-muted" />{copy.handoffTitle}</h3>
      <p className="mt-1 text-[10.5px] text-text-faint">{copy.handoffSubtitle}</p>
      {failed ? <p className="mt-3 text-[11.5px] text-red-500">{copy.handoffLoadFailed}</p> : rows.length === 0 ? <p className="mt-3 text-[11.5px] text-text-faint">{copy.noHandoffs}</p> : (
        <div className="mt-3 space-y-3">
          {rows.map((row) => (
            <div key={row.id} className="rounded-lg border border-border bg-bg p-3">
              <div className="flex items-center justify-between gap-2"><span className="text-[12px] font-medium">{row.position_title}</span><span className="text-[10px] text-text-faint">{copy.handoffStatuses[row.status] ?? row.status}</span></div>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {items.map((item) => (
                  <label key={item} className="flex items-center gap-2 text-[11px] text-text-muted">
                    <input type="checkbox" checked={Boolean(row.checklist[item])} disabled={pending || !canManage} onChange={(event) => onChange(row, item, event.target.checked)} />
                    {copy.checklist[item] ?? item}
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block"><span className="mb-1 block text-[10.5px] font-medium text-text-muted">{label}</span>{children}</label>;
}

function Action({ icon, label, disabled, onClick }: { icon: React.ReactElement<{ className?: string }>; label: string; disabled: boolean; onClick: () => void }) {
  return (
    <button type="button" disabled={disabled} onClick={onClick} className="inline-flex min-h-8 items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-2.5 text-[10.5px] text-text-muted hover:bg-bg-hover disabled:opacity-50 [&_svg]:h-3.5 [&_svg]:w-3.5">
      {icon}
      {label}
    </button>
  );
}
