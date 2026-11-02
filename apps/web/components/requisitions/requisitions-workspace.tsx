"use client";

import Link from "next/link";
import {
  BriefcaseBusiness,
  Check,
  CircleDollarSign,
  FilePlus2,
  Loader2,
  LockKeyhole,
  Pencil,
  Plus,
  RefreshCw,
  Send,
  UserRoundCheck,
  X,
  XCircle,
} from "lucide-react";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  cancelRequisitionAction,
  createRequisitionAction,
  listRequisitionsAction,
  openRequisitionAction,
  submitRequisitionAction,
  updateRequisitionAction,
  voteRequisitionAction,
} from "@/lib/actions";
import type { AssignableUser, Requisition } from "@/lib/types";
import { cn } from "@/lib/utils";

const controlClass =
  "min-h-9 w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[12px] text-text outline-none transition-colors focus:border-border-strong";

/** @ui-only Requisition editor state. */
type FormState = {
  title: string;
  department: string;
  justification: string;
  headcount: string;
  managerId: string;
  salaryMin: string;
  salaryMax: string;
  currency: string;
  confidential: boolean;
  requiredApprovals: string;
};

const EMPTY_FORM: FormState = {
  title: "",
  department: "",
  justification: "",
  headcount: "1",
  managerId: "",
  salaryMin: "",
  salaryMax: "",
  currency: "TJS",
  confidential: true,
  requiredApprovals: "1",
};

function decimalInput(value: string | number | null): string {
  return value == null ? "" : String(value);
}

export function RequisitionsWorkspace({
  initialRequisitions,
  users,
  currentUserId,
  canManage,
  canApprove,
  canViewCompensation,
  initialLoadFailed,
}: {
  initialRequisitions: Requisition[];
  users: AssignableUser[];
  currentUserId: string;
  canManage: boolean;
  canApprove: boolean;
  canViewCompensation: boolean;
  initialLoadFailed: boolean;
}) {
  const { m } = useI18n();
  const copy = m.requisitions;
  const [requisitions, setRequisitions] = useState(initialRequisitions);
  const [loadFailed, setLoadFailed] = useState(initialLoadFailed);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [pending, startTransition] = useTransition();

  const salaryMin = form.salaryMin === "" ? null : Number(form.salaryMin);
  const salaryMax = form.salaryMax === "" ? null : Number(form.salaryMax);
  const salaryValid =
    (!canViewCompensation ||
      ((salaryMin == null || (Number.isFinite(salaryMin) && salaryMin >= 0)) &&
        (salaryMax == null || (Number.isFinite(salaryMax) && salaryMax >= 0)) &&
        (salaryMin == null || salaryMax == null || salaryMin <= salaryMax))) &&
    /^[A-Z]{3}$/.test(form.currency);
  const formValid =
    form.title.trim().length > 0 &&
    form.justification.trim().length > 0 &&
    Number.isInteger(Number(form.headcount)) &&
    Number(form.headcount) >= 1 &&
    Number(form.headcount) <= 10_000 &&
    Number.isInteger(Number(form.requiredApprovals)) &&
    Number(form.requiredApprovals) >= 1 &&
    Number(form.requiredApprovals) <= 20 &&
    salaryValid;

  function replace(next: Requisition) {
    setRequisitions((current) => {
      const exists = current.some((item) => item.id === next.id);
      return exists
        ? current.map((item) => (item.id === next.id ? next : item))
        : [next, ...current];
    });
  }

  function resetForm() {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setFormOpen(false);
  }

  function edit(requisition: Requisition) {
    setEditingId(requisition.id);
    setForm({
      title: requisition.title,
      department: requisition.department ?? "",
      justification: requisition.business_justification,
      headcount: String(requisition.headcount),
      managerId: requisition.hiring_manager_user_id ?? "",
      salaryMin: requisition.compensation_visible
        ? decimalInput(requisition.salary_min)
        : "",
      salaryMax: requisition.compensation_visible
        ? decimalInput(requisition.salary_max)
        : "",
      currency: requisition.salary_currency ?? "TJS",
      confidential: requisition.salary_confidential,
      requiredApprovals: String(requisition.required_approvals),
    });
    setFormOpen(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function save(event: React.FormEvent) {
    event.preventDefault();
    if (!formValid || pending || !canManage) return;
    const common = {
      title: form.title.trim(),
      department: form.department.trim() || null,
      business_justification: form.justification.trim(),
      headcount: Number(form.headcount),
      hiring_manager_user_id: form.managerId || null,
      required_approvals: Number(form.requiredApprovals),
    };
    const compensation = canViewCompensation
      ? {
          salary_min: salaryMin,
          salary_max: salaryMax,
          salary_currency: form.currency,
          salary_confidential: form.confidential,
        }
      : {};
    startTransition(async () => {
      const result = editingId
        ? await updateRequisitionAction(editingId, {
            ...common,
            ...compensation,
          })
        : await createRequisitionAction({
            ...common,
            ...compensation,
            salary_confidential: canViewCompensation
              ? form.confidential
              : true,
          });
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      replace(result.data);
      toast.success(editingId ? copy.updated : copy.created);
      resetForm();
    });
  }

  function retry() {
    startTransition(async () => {
      const result = await listRequisitionsAction();
      if (!result.ok) {
        setLoadFailed(true);
        return;
      }
      setRequisitions(result.data);
      setLoadFailed(false);
    });
  }

  return (
    <div className="mx-auto max-w-[1180px] space-y-6">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-[24px] font-semibold tracking-[-0.025em] text-text">{copy.title}</h1>
          <p className="mt-1 text-[12px] text-text-muted">{copy.subtitle}</p>
        </div>
        {canManage ? (
          <Button variant={formOpen ? "ghost" : "default"} onClick={() => (formOpen ? resetForm() : setFormOpen(true))}>
            {formOpen ? <X className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
            {formOpen ? m.common.cancel : copy.newRequisition}
          </Button>
        ) : null}
      </header>

      {!canManage ? (
        <Notice icon={<LockKeyhole className="h-4 w-4" />} text={copy.manageRestricted} />
      ) : null}
      {!canViewCompensation ? (
        <Notice icon={<CircleDollarSign className="h-4 w-4" />} text={copy.compensationRestricted} />
      ) : null}
      {loadFailed ? (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-score-mid/30 bg-score-mid/5 px-4 py-3 text-[11px] text-score-mid">
          <span>{copy.loadFailed}</span>
          <Button size="sm" variant="ghost" onClick={retry} disabled={pending}>
            <RefreshCw className={cn("h-3.5 w-3.5", pending && "animate-spin")} /> {copy.retry}
          </Button>
        </div>
      ) : null}

      {formOpen ? (
        <form onSubmit={save} className="rounded-xl border border-border bg-bg-elevated p-5">
          <div className="mb-4 flex items-center gap-2">
            <FilePlus2 className="h-4 w-4 text-primary" />
            <h2 className="text-[13px] font-semibold text-text">{editingId ? copy.editTitle : copy.createTitle}</h2>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <Field label={copy.titleLabel}>
              <input value={form.title} onChange={(event) => setForm((current) => ({ ...current, title: event.target.value }))} maxLength={255} placeholder={copy.titlePlaceholder} className={controlClass} />
            </Field>
            <Field label={copy.department}>
              <input value={form.department} onChange={(event) => setForm((current) => ({ ...current, department: event.target.value }))} maxLength={160} placeholder={copy.departmentPlaceholder} className={controlClass} />
            </Field>
            <label className="flex flex-col gap-1.5 md:col-span-2">
              <span className="text-[10.5px] font-medium text-text-muted">{copy.justification}</span>
              <textarea value={form.justification} onChange={(event) => setForm((current) => ({ ...current, justification: event.target.value }))} maxLength={20_000} rows={4} placeholder={copy.justificationPlaceholder} className={controlClass} />
            </label>
            <Field label={copy.headcount}>
              <input type="number" min={1} max={10_000} value={form.headcount} onChange={(event) => setForm((current) => ({ ...current, headcount: event.target.value }))} className={controlClass} />
            </Field>
            <Field label={copy.hiringManager}>
              <select value={form.managerId} onChange={(event) => setForm((current) => ({ ...current, managerId: event.target.value }))} className={controlClass}>
                <option value="">{copy.unassignedManager}</option>
                {users.map((user) => <option key={user.id} value={user.id}>{user.email}</option>)}
              </select>
            </Field>
            <Field label={copy.requiredApprovals}>
              <input type="number" min={1} max={20} value={form.requiredApprovals} onChange={(event) => setForm((current) => ({ ...current, requiredApprovals: event.target.value }))} className={controlClass} />
            </Field>
          </div>

          {canViewCompensation ? (
            <fieldset className="mt-5 rounded-[8px] border border-border bg-bg p-4">
              <legend className="px-1 text-[10.5px] font-semibold uppercase tracking-[0.07em] text-text-muted">{copy.salary}</legend>
              <div className="grid gap-3 sm:grid-cols-3">
                <Field label={copy.salaryMin}><input type="number" min={0} step="0.01" value={form.salaryMin} onChange={(event) => setForm((current) => ({ ...current, salaryMin: event.target.value }))} className={controlClass} /></Field>
                <Field label={copy.salaryMax}><input type="number" min={0} step="0.01" value={form.salaryMax} onChange={(event) => setForm((current) => ({ ...current, salaryMax: event.target.value }))} className={controlClass} /></Field>
                <Field label={copy.currency}><input value={form.currency} onChange={(event) => setForm((current) => ({ ...current, currency: event.target.value.toUpperCase().slice(0, 3) }))} maxLength={3} className={controlClass} /></Field>
              </div>
              <label className="mt-3 flex items-center gap-2 text-[11px] text-text-muted">
                <input type="checkbox" checked={form.confidential} onChange={(event) => setForm((current) => ({ ...current, confidential: event.target.checked }))} />
                {copy.confidential}
              </label>
            </fieldset>
          ) : null}

          <div className="mt-4 flex justify-end">
            <Button type="submit" disabled={pending || !formValid}>
              {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <FilePlus2 className="h-4 w-4" />}
              {pending ? copy.creating : editingId ? m.common.save : copy.create}
            </Button>
          </div>
        </form>
      ) : null}

      {requisitions.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border px-5 py-14 text-center text-[12px] text-text-muted">{copy.empty}</div>
      ) : (
        <div className="space-y-3">
          {requisitions.map((requisition) => (
            <RequisitionCard
              key={`${requisition.id}:${requisition.updated_at}`}
              requisition={requisition}
              users={users}
              currentUserId={currentUserId}
              canManage={canManage}
              canApprove={canApprove}
              canViewCompensation={canViewCompensation}
              pending={pending}
              onEdit={() => edit(requisition)}
              onReplace={replace}
              run={startTransition}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function RequisitionCard({
  requisition,
  users,
  currentUserId,
  canManage,
  canApprove,
  canViewCompensation,
  pending,
  onEdit,
  onReplace,
  run,
}: {
  requisition: Requisition;
  users: AssignableUser[];
  currentUserId: string;
  canManage: boolean;
  canApprove: boolean;
  canViewCompensation: boolean;
  pending: boolean;
  onEdit: () => void;
  onReplace: (value: Requisition) => void;
  run: React.TransitionStartFunction;
}) {
  const { locale, m } = useI18n();
  const copy = m.requisitions;
  const [comment, setComment] = useState(
    requisition.approvals.find((approval) => approval.user_id === currentUserId)?.comment ?? "",
  );
  const [openForm, setOpenForm] = useState(false);
  const [description, setDescription] = useState("");
  const [requirements, setRequirements] = useState("");
  const [publicApply, setPublicApply] = useState(true);
  const manager = users.find((user) => user.id === requisition.hiring_manager_user_id);
  const approveCount = requisition.approvals.filter((approval) => approval.vote === "approve").length;
  const money = formatSalary(requisition, locale, copy.confidentialValue);

  function transition(action: () => Promise<{ ok: true; data: Requisition } | { ok: false; error: string }>, success: string) {
    run(async () => {
      const result = await action();
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      onReplace(result.data);
      toast.success(success);
    });
  }

  function openPosition(event: React.FormEvent) {
    event.preventDefault();
    let parsed: Record<string, unknown> | null = null;
    if (requirements.trim()) {
      try {
        const value: unknown = JSON.parse(requirements);
        if (!value || Array.isArray(value) || typeof value !== "object") throw new Error();
        parsed = value as Record<string, unknown>;
      } catch {
        toast.error(copy.invalidRequirements);
        return;
      }
    }
    transition(
      () => openRequisitionAction(requisition.id, { description: description.trim() || null, requirements: parsed, public_apply_enabled: publicApply }),
      copy.opened,
    );
    setOpenForm(false);
  }

  return (
    <article className="rounded-xl border border-border bg-bg-elevated p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-[14px] font-semibold text-text">{requisition.title}</h2>
            <span className={cn("rounded-[4px] px-2 py-1 text-[9.5px] font-medium", requisition.status === "approved" || requisition.status === "opened" ? "bg-accent-muted text-primary" : requisition.status === "rejected" || requisition.status === "cancelled" ? "bg-destructive/10 text-destructive" : "bg-bg text-text-muted")}>{copy.statuses[requisition.status]}</span>
          </div>
          <p className="mt-1 text-[10.5px] text-text-faint">{[requisition.department, copy.createdAt(m.format.relativeTime(requisition.created_at))].filter(Boolean).join(" · ")}</p>
          <p className="mt-3 whitespace-pre-wrap text-[11.5px] leading-5 text-text-muted">{requisition.business_justification}</p>
          <dl className="mt-4 grid gap-2 text-[10.5px] sm:grid-cols-2 lg:grid-cols-4">
            <Meta label={copy.headcount} value={String(requisition.headcount)} />
            <Meta label={copy.hiringManager} value={manager?.email ?? copy.unassignedManager} />
            <Meta label={copy.salary} value={money} locked={!requisition.compensation_visible && requisition.salary_confidential} />
            <Meta label={copy.requiredApprovals} value={copy.approvals(approveCount, requisition.required_approvals)} />
          </dl>
        </div>
        {canManage ? (
          <div className="flex shrink-0 flex-wrap gap-2">
            {requisition.status === "draft" ? <Button size="sm" variant="ghost" onClick={onEdit} disabled={pending}><Pencil className="h-3.5 w-3.5" />{m.common.edit}</Button> : null}
            {requisition.status === "draft" ? <Button size="sm" onClick={() => window.confirm(copy.submitConfirm) && transition(() => submitRequisitionAction(requisition.id), copy.submitted)} disabled={pending}>{pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}{pending ? copy.submitting : copy.submit}</Button> : null}
            {requisition.status === "approved" ? <Button size="sm" onClick={() => setOpenForm((value) => !value)} disabled={pending}><BriefcaseBusiness className="h-3.5 w-3.5" />{copy.openPosition}</Button> : null}
            {requisition.position_id ? <Button size="sm" variant="outline" render={<Link href={`/positions/${requisition.position_id}`} />}><BriefcaseBusiness className="h-3.5 w-3.5" />{copy.viewPosition}</Button> : null}
            {["draft", "pending", "approved"].includes(requisition.status) ? <Button size="sm" variant="destructive" onClick={() => window.confirm(copy.cancelConfirm) && transition(() => cancelRequisitionAction(requisition.id), copy.cancelled)} disabled={pending}>{copy.cancelRequisition}</Button> : null}
          </div>
        ) : null}
      </div>

      {requisition.approvals.length ? (
        <div className="mt-4 space-y-1.5 border-t border-border pt-3">
          {requisition.approvals.map((approval) => (
            <div key={approval.id} className="rounded-[5px] bg-bg px-2.5 py-2 text-[10.5px] text-text-muted">
              <span className="font-medium text-text">{approval.user_email}</span> · {approval.vote === "approve" ? copy.approve : copy.reject}
              {approval.comment ? <p className="mt-1 text-text-faint">{approval.comment}</p> : null}
            </div>
          ))}
        </div>
      ) : null}

      {requisition.status === "pending" && canApprove ? (
        <div className="mt-4 border-t border-border pt-3">
          <input value={comment} onChange={(event) => setComment(event.target.value)} maxLength={4_000} placeholder={copy.voteCommentPlaceholder} className={controlClass} />
          <div className="mt-2 flex justify-end gap-2">
            <Button size="sm" variant="destructive" disabled={pending} onClick={() => transition(() => voteRequisitionAction(requisition.id, { vote: "reject", comment: comment.trim() || null }), copy.voted)}><XCircle className="h-3.5 w-3.5" />{copy.reject}</Button>
            <Button size="sm" disabled={pending} onClick={() => transition(() => voteRequisitionAction(requisition.id, { vote: "approve", comment: comment.trim() || null }), copy.voted)}><UserRoundCheck className="h-3.5 w-3.5" />{pending ? copy.voting : copy.approve}</Button>
          </div>
        </div>
      ) : null}

      {openForm ? (
        <form onSubmit={openPosition} className="mt-4 rounded-[8px] border border-border bg-bg p-4">
          <h3 className="text-[12px] font-semibold text-text">{copy.openTitle}</h3>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <label className="flex flex-col gap-1.5"><span className="text-[10.5px] text-text-muted">{copy.positionDescription}</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={4} maxLength={20_000} placeholder={copy.positionDescriptionPlaceholder} className={controlClass} /></label>
            <label className="flex flex-col gap-1.5"><span className="text-[10.5px] text-text-muted">{copy.requirementsJson}</span><textarea value={requirements} onChange={(event) => setRequirements(event.target.value)} rows={4} placeholder={copy.requirementsJsonPlaceholder} className={`${controlClass} font-mono`} /></label>
          </div>
          <label className="mt-3 flex items-center gap-2 text-[11px] text-text-muted"><input type="checkbox" checked={publicApply} onChange={(event) => setPublicApply(event.target.checked)} />{copy.publicApply}</label>
          <div className="mt-3 flex justify-end gap-2"><Button type="button" size="sm" variant="ghost" onClick={() => setOpenForm(false)}>{m.common.cancel}</Button><Button type="submit" size="sm" disabled={pending}>{pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BriefcaseBusiness className="h-3.5 w-3.5" />}{pending ? copy.opening : copy.openPosition}</Button></div>
        </form>
      ) : null}
    </article>
  );
}

function formatSalary(requisition: Requisition, locale: "ru" | "en", confidential: string): string {
  if (!requisition.compensation_visible) return confidential;
  if (requisition.salary_min == null && requisition.salary_max == null) return "—";
  const formatter = new Intl.NumberFormat(locale === "ru" ? "ru-RU" : "en-US", { maximumFractionDigits: 2 });
  const min = requisition.salary_min == null ? null : formatter.format(Number(requisition.salary_min));
  const max = requisition.salary_max == null ? null : formatter.format(Number(requisition.salary_max));
  return `${min ?? "—"} – ${max ?? "—"} ${requisition.salary_currency ?? ""}`.trim();
}

function Meta({ label, value, locked = false }: { label: string; value: string; locked?: boolean }) {
  return <div className="rounded-[6px] bg-bg px-2.5 py-2"><dt className="flex items-center gap-1 text-[9.5px] uppercase tracking-[0.06em] text-text-faint">{locked ? <LockKeyhole className="h-3 w-3" /> : null}{label}</dt><dd className="mt-1 truncate text-[11px] font-medium text-text">{value}</dd></div>;
}

function Notice({ icon, text }: { icon: React.ReactNode; text: string }) {
  return <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated px-4 py-3 text-[11px] text-text-muted">{icon}<span>{text}</span></div>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="flex min-w-0 flex-col gap-1.5"><span className="text-[10.5px] font-medium text-text-muted">{label}</span>{children}</label>;
}
