"use client";

import { ClipboardCheck, Loader2, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  createScorecardTemplateAction,
  listScorecardTemplatesAction,
  updateScorecardTemplateAction,
} from "@/lib/actions";
import type { Position, ScorecardCriterionCreate, ScorecardTemplate } from "@/lib/types";
import { cn } from "@/lib/utils";

/** @ui-only Scorecard criterion with a stable client editor key. */
type DraftCriterion = ScorecardCriterionCreate & { clientId: string };

const controlClass =
  "min-h-9 w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[12px] text-text outline-none transition-colors focus:border-border-strong";

function blankCriterion(index = 1): DraftCriterion {
  return {
    clientId: `criterion-${index}`,
    key: "",
    label: "",
    description: "",
    weight: 1,
    is_required: true,
  };
}

function normalizedCriterionKey(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

export function ScorecardTemplatesTab({
  initialTemplates,
  positions,
  initialLoadFailed,
  canManage,
}: {
  initialTemplates: ScorecardTemplate[];
  positions: Position[];
  initialLoadFailed: boolean;
  canManage: boolean;
}) {
  const { m } = useI18n();
  const copy = m.scorecards;
  const [templates, setTemplates] = useState(initialTemplates);
  const [loadFailed, setLoadFailed] = useState(initialLoadFailed);
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [positionId, setPositionId] = useState("");
  const [criteria, setCriteria] = useState<DraftCriterion[]>([blankCriterion()]);
  const [pending, startTransition] = useTransition();

  const positionNames = new Map(positions.map((position) => [position.id, position.title]));
  const normalizedKeys = criteria.map((criterion) =>
    normalizedCriterionKey(criterion.key),
  );
  const valid =
    name.trim().length > 0 &&
    criteria.length > 0 &&
    criteria.length <= 30 &&
    new Set(normalizedKeys).size === normalizedKeys.length &&
    criteria.every(
      (criterion) =>
        normalizedCriterionKey(criterion.key).length > 0 &&
        criterion.label.trim().length > 0 &&
        Number.isInteger(Number(criterion.weight)) &&
        Number(criterion.weight) >= 1 &&
        Number(criterion.weight) <= 100,
    );

  function resetForm() {
    setName("");
    setDescription("");
    setPositionId("");
    setCriteria([blankCriterion()]);
    setFormOpen(false);
  }

  function addCriterion() {
    setCriteria((current) => [
      ...current,
      blankCriterion(current.length + 1 + Date.now()),
    ]);
  }

  function updateCriterion(clientId: string, patch: Partial<DraftCriterion>) {
    setCriteria((current) =>
      current.map((criterion) =>
        criterion.clientId === clientId ? { ...criterion, ...patch } : criterion,
      ),
    );
  }

  function removeCriterion(clientId: string) {
    setCriteria((current) => current.filter((criterion) => criterion.clientId !== clientId));
  }

  function createTemplate(event: React.FormEvent) {
    event.preventDefault();
    if (!canManage || !valid || pending) return;
    startTransition(async () => {
      const result = await createScorecardTemplateAction({
        name: name.trim(),
        description: description.trim() || null,
        position_id: positionId || null,
        criteria: criteria.map(({ clientId: _clientId, ...criterion }) => ({
          ...criterion,
          key: normalizedCriterionKey(criterion.key),
          label: criterion.label.trim(),
          description: criterion.description?.trim() || null,
          weight: Number(criterion.weight) || 1,
          is_required: criterion.is_required ?? true,
        })),
      });
      if (!result.ok) {
        toast.error(copy.templateSaveFailed);
        return;
      }
      setTemplates((current) => [result.data, ...current]);
      setLoadFailed(false);
      resetForm();
      toast.success(copy.templateCreated);
    });
  }

  function retry() {
    startTransition(async () => {
      const result = await listScorecardTemplatesAction({ includeInactive: true });
      if (!result.ok) {
        setLoadFailed(true);
        return;
      }
      setTemplates(result.data);
      setLoadFailed(false);
    });
  }

  function replaceTemplate(template: ScorecardTemplate) {
    setTemplates((current) =>
      current.map((item) => (item.id === template.id ? template : item)),
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-[16px] font-semibold text-text">{copy.settingsTitle}</h2>
          <p className="mt-1 text-[12px] text-text-muted">{copy.settingsSubtitle}</p>
        </div>
        {canManage ? <Button size="sm" variant={formOpen ? "ghost" : "outline"} onClick={() => setFormOpen((current) => !current)}>
          {formOpen ? <X className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
          {formOpen ? m.common.cancel : copy.newTemplate}
        </Button> : null}
      </div>

      {loadFailed ? (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-score-mid/30 bg-score-mid/5 px-4 py-3 text-[11px] text-score-mid">
          <span>{copy.settingsLoadFailed}</span>
          <Button size="sm" variant="ghost" onClick={retry} disabled={pending}>
            <RefreshCw className={cn("h-3.5 w-3.5", pending && "animate-spin")} />
            {copy.retry}
          </Button>
        </div>
      ) : null}

      {canManage && formOpen ? (
        <form onSubmit={createTemplate} className="rounded-xl border border-border bg-bg-elevated p-5">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label={copy.templateName}>
              <input value={name} onChange={(event) => setName(event.target.value)} maxLength={160} placeholder={copy.templateNamePlaceholder} className={controlClass} />
            </Field>
            <Field label={copy.position}>
              <select value={positionId} onChange={(event) => setPositionId(event.target.value)} className={controlClass}>
                <option value="">{copy.globalTemplate}</option>
                {positions.filter((position) => position.status !== "closed").map((position) => (
                  <option key={position.id} value={position.id}>{position.title}</option>
                ))}
              </select>
            </Field>
            <label className="flex flex-col gap-1.5 md:col-span-2">
              <span className="text-[10.5px] font-medium text-text-muted">{copy.templateDescription}</span>
              <textarea value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} rows={2} placeholder={copy.templateDescriptionPlaceholder} className={controlClass} />
            </label>
          </div>

          <div className="mt-5 flex items-center justify-between gap-3">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.07em] text-text-muted">{copy.criteria}</h3>
            <Button type="button" size="sm" variant="ghost" onClick={addCriterion} disabled={criteria.length >= 30}>
              <Plus className="h-3.5 w-3.5" /> {copy.addCriterion}
            </Button>
          </div>
          <div className="mt-2 space-y-3">
            {criteria.map((criterion, index) => (
              <div key={criterion.clientId} className="rounded-[8px] border border-border bg-bg p-3.5">
                <div className="grid gap-3 md:grid-cols-[minmax(0,.7fr)_minmax(0,1.3fr)_90px_auto]">
                  <Field label={copy.criterionKey}>
                    <input value={criterion.key} onChange={(event) => updateCriterion(criterion.clientId, { key: event.target.value })} maxLength={80} className={controlClass} />
                  </Field>
                  <Field label={copy.criterionLabel}>
                    <input value={criterion.label} onChange={(event) => updateCriterion(criterion.clientId, { label: event.target.value })} maxLength={160} className={controlClass} />
                  </Field>
                  <Field label={copy.criterionWeight}>
                    <input type="number" min={1} max={100} step={1} value={criterion.weight} onChange={(event) => updateCriterion(criterion.clientId, { weight: Number(event.target.value) })} className={controlClass} />
                  </Field>
                  <div className="flex items-end justify-end">
                    <Button type="button" size="icon-sm" variant="ghost" title={copy.removeCriterion} onClick={() => removeCriterion(criterion.clientId)} disabled={criteria.length === 1}>
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
                <div className="mt-3 grid gap-3 md:grid-cols-[1fr_auto]">
                  <Field label={copy.criterionDescription}>
                    <input value={criterion.description ?? ""} onChange={(event) => updateCriterion(criterion.clientId, { description: event.target.value })} maxLength={1000} className={controlClass} />
                  </Field>
                  <label className="flex items-end gap-2 pb-2 text-[11px] text-text-muted">
                    <input type="checkbox" checked={criterion.is_required ?? true} onChange={(event) => updateCriterion(criterion.clientId, { is_required: event.target.checked })} />
                    {copy.required}
                  </label>
                </div>
                <span className="sr-only">{index + 1}</span>
              </div>
            ))}
          </div>
          <p className="mt-3 text-[10.5px] leading-4 text-text-faint">{copy.criteriaImmutable}</p>
          <div className="mt-4 flex justify-end">
            <Button type="submit" size="sm" disabled={pending || !valid}>
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ClipboardCheck className="h-3.5 w-3.5" />}
              {pending ? copy.creatingTemplate : copy.createTemplate}
            </Button>
          </div>
        </form>
      ) : null}

      {!loadFailed && templates.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border px-5 py-12 text-center text-[12px] text-text-muted">{copy.noTemplatesConfigured}</div>
      ) : templates.length > 0 ? (
        <div className="grid gap-3 lg:grid-cols-2">
          {templates.map((template) => (
            <TemplateCard key={template.id} template={template} positionName={template.position_id ? positionNames.get(template.position_id) ?? null : null} onUpdate={replaceTemplate} canManage={canManage} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function TemplateCard({
  template,
  positionName,
  onUpdate,
  canManage,
}: {
  template: ScorecardTemplate;
  positionName: string | null;
  onUpdate: (template: ScorecardTemplate) => void;
  canManage: boolean;
}) {
  const { m } = useI18n();
  const copy = m.scorecards;
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(template.name);
  const [description, setDescription] = useState(template.description ?? "");
  const [pending, startTransition] = useTransition();

  function save(event: React.FormEvent) {
    event.preventDefault();
    if (!canManage || !name.trim() || pending) return;
    startTransition(async () => {
      const result = await updateScorecardTemplateAction(template.id, {
        name: name.trim(),
        description: description.trim() || null,
      });
      if (!result.ok) {
        toast.error(copy.templateSaveFailed);
        return;
      }
      onUpdate(result.data);
      setEditing(false);
      toast.success(copy.templateUpdated);
    });
  }

  function toggleActive() {
    if (!canManage) return;
    startTransition(async () => {
      const result = await updateScorecardTemplateAction(template.id, { is_active: !template.is_active });
      if (!result.ok) {
        toast.error(copy.templateSaveFailed);
        return;
      }
      onUpdate(result.data);
      toast.success(copy.templateUpdated);
    });
  }

  return (
    <article className="rounded-xl border border-border bg-bg-elevated p-5">
      {canManage && editing ? (
        <form onSubmit={save} className="space-y-3">
          <Field label={copy.templateName}>
            <input value={name} onChange={(event) => setName(event.target.value)} maxLength={160} className={controlClass} />
          </Field>
          <Field label={copy.templateDescription}>
            <textarea value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} rows={2} className={controlClass} />
          </Field>
          <p className="text-[10.5px] leading-4 text-text-faint">{copy.criteriaImmutable}</p>
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>{m.common.cancel}</Button>
            <Button type="submit" size="sm" disabled={pending || !name.trim()}>
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              {m.common.save}
            </Button>
          </div>
        </form>
      ) : (
        <>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-[13px] font-semibold text-text">{template.name}</h3>
                <span className={cn("rounded-[4px] px-1.5 py-0.5 text-[9.5px]", template.is_active ? "bg-accent-muted text-primary" : "bg-bg-active text-text-faint")}>
                  {template.is_active ? copy.active : copy.inactive}
                </span>
              </div>
              {template.description ? <p className="mt-1 text-[11px] leading-4 text-text-muted">{template.description}</p> : null}
              <p className="mt-1 text-[10px] text-text-faint">{positionName ?? copy.globalTemplate}</p>
            </div>
            {canManage ? <Button size="icon-sm" variant="ghost" title={m.common.edit} onClick={() => setEditing(true)}>
              <Pencil className="h-3.5 w-3.5" />
            </Button> : null}
          </div>
          <div className="mt-4 space-y-1.5">
            {template.criteria.map((criterion) => (
              <div key={criterion.id} className="flex items-start justify-between gap-3 rounded-[6px] bg-bg px-2.5 py-2">
                <div>
                  <p className="text-[11px] font-medium text-text">{criterion.label}{criterion.is_required ? " *" : ""}</p>
                  {criterion.description ? <p className="mt-0.5 text-[10px] leading-4 text-text-faint">{criterion.description}</p> : null}
                </div>
                <span className="shrink-0 font-mono text-[9.5px] text-text-faint">×{criterion.weight}</span>
              </div>
            ))}
          </div>
          {canManage ? <div className="mt-4 flex justify-end">
            <Button size="sm" variant="ghost" onClick={toggleActive} disabled={pending}>
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              {template.is_active ? copy.inactive : copy.active}
            </Button>
          </div> : null}
        </>
      )}
    </article>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="flex min-w-0 flex-col gap-1.5"><span className="text-[10.5px] font-medium text-text-muted">{label}</span>{children}</label>;
}
