"use client";

import { FileSignature, Loader2, Pencil, Plus, RefreshCw, X } from "lucide-react";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  createOfferTemplateAction,
  listOfferTemplatesAction,
  updateOfferTemplateAction,
} from "@/lib/actions";
import type { OfferTemplate } from "@/lib/types";
import { cn } from "@/lib/utils";

const controlClass =
  "min-h-9 w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[12px] text-text outline-none transition-colors focus:border-border-strong";

export function OfferTemplatesTab({
  initialTemplates,
  initialLoadFailed,
  canManage,
}: {
  initialTemplates: OfferTemplate[];
  initialLoadFailed: boolean;
  canManage: boolean;
}) {
  const { m } = useI18n();
  const copy = m.offers;
  const [templates, setTemplates] = useState(initialTemplates);
  const [loadFailed, setLoadFailed] = useState(initialLoadFailed);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [pending, startTransition] = useTransition();
  const valid = Boolean(name.trim() && subject.trim() && body.trim());

  function reset() {
    setName("");
    setSubject("");
    setBody("");
    setEditingId(null);
    setFormOpen(false);
  }

  function edit(template: OfferTemplate) {
    if (!canManage) return;
    setName(template.name);
    setSubject(template.subject_template);
    setBody(template.body_template);
    setEditingId(template.id);
    setFormOpen(true);
  }

  function replace(template: OfferTemplate) {
    setTemplates((current) => {
      const exists = current.some((item) => item.id === template.id);
      return exists
        ? current.map((item) => (item.id === template.id ? template : item))
        : [template, ...current];
    });
  }

  function save(event: React.FormEvent) {
    event.preventDefault();
    if (!canManage || !valid || pending) return;
    startTransition(async () => {
      const values = {
        name: name.trim(),
        subject_template: subject.trim(),
        body_template: body.trim(),
      };
      const result = editingId
        ? await updateOfferTemplateAction(editingId, values)
        : await createOfferTemplateAction(values);
      if (!result.ok) {
        toast.error(copy.templateSaveFailed);
        return;
      }
      replace(result.data);
      toast.success(editingId ? copy.templateUpdated : copy.templateCreated);
      reset();
    });
  }

  function toggle(template: OfferTemplate) {
    if (!canManage) return;
    startTransition(async () => {
      const result = await updateOfferTemplateAction(template.id, {
        is_active: !template.is_active,
      });
      if (!result.ok) {
        toast.error(copy.templateSaveFailed);
        return;
      }
      replace(result.data);
      toast.success(copy.templateUpdated);
    });
  }

  function retry() {
    startTransition(async () => {
      const result = await listOfferTemplatesAction();
      if (!result.ok) {
        setLoadFailed(true);
        return;
      }
      setTemplates(result.data);
      setLoadFailed(false);
    });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-[16px] font-semibold text-text">{copy.templatesTitle}</h2>
          <p className="mt-1 text-[12px] text-text-muted">{copy.templatesSubtitle}</p>
        </div>
        {canManage ? <Button size="sm" variant={formOpen ? "ghost" : "outline"} onClick={() => (formOpen ? reset() : setFormOpen(true))}>
          {formOpen ? <X className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
          {formOpen ? m.common.cancel : copy.newTemplate}
        </Button> : null}
      </div>

      {loadFailed ? (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-score-mid/30 bg-score-mid/5 px-4 py-3 text-[11px] text-score-mid">
          <span>{copy.templatesLoadFailed}</span>
          <Button size="sm" variant="ghost" onClick={retry} disabled={pending}><RefreshCw className={cn("h-3.5 w-3.5", pending && "animate-spin")} />{copy.retry}</Button>
        </div>
      ) : null}

      {canManage && formOpen ? (
        <form onSubmit={save} className="rounded-xl border border-border bg-bg-elevated p-5">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label={copy.templateName}><input value={name} onChange={(event) => setName(event.target.value)} maxLength={160} placeholder={copy.templateNamePlaceholder} className={controlClass} /></Field>
            <Field label={copy.subjectTemplate}><input value={subject} onChange={(event) => setSubject(event.target.value)} maxLength={500} placeholder={copy.subjectPlaceholder} className={controlClass} /></Field>
            <label className="flex flex-col gap-1.5 md:col-span-2"><span className="text-[10.5px] font-medium text-text-muted">{copy.bodyTemplate}</span><textarea value={body} onChange={(event) => setBody(event.target.value)} maxLength={30_000} rows={10} placeholder={copy.bodyPlaceholder} className={controlClass} /></label>
          </div>
          <p className="mt-3 font-mono text-[10px] leading-4 text-text-faint">{copy.variablesHint}</p>
          <div className="mt-4 flex justify-end">
            <Button type="submit" size="sm" disabled={pending || !valid}>{pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileSignature className="h-3.5 w-3.5" />}{pending ? copy.creatingTemplate : editingId ? m.common.save : copy.createTemplate}</Button>
          </div>
        </form>
      ) : null}

      {templates.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border px-5 py-12 text-center text-[12px] text-text-muted">{copy.noTemplates}</div>
      ) : (
        <div className="grid gap-3 lg:grid-cols-2">
          {templates.map((template) => (
            <article key={template.id} className="rounded-xl border border-border bg-bg-elevated p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><h3 className="text-[13px] font-semibold text-text">{template.name}</h3><span className={cn("rounded-[4px] px-1.5 py-0.5 text-[9.5px]", template.is_active ? "bg-accent-muted text-primary" : "bg-bg-active text-text-faint")}>{template.is_active ? copy.active : copy.inactive}</span></div>
                  <p className="mt-2 text-[11px] font-medium text-text-muted">{template.subject_template}</p>
                </div>
                {canManage ? <Button size="icon-sm" variant="ghost" title={m.common.edit} onClick={() => edit(template)}><Pencil className="h-3.5 w-3.5" /></Button> : null}
              </div>
              <p className="mt-3 max-h-32 overflow-hidden whitespace-pre-wrap rounded-[6px] bg-bg px-3 py-2.5 text-[10.5px] leading-4 text-text-faint">{template.body_template}</p>
              {canManage ? <div className="mt-3 flex justify-end"><Button size="sm" variant="ghost" onClick={() => toggle(template)} disabled={pending}>{pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}{template.is_active ? copy.inactive : copy.active}</Button></div> : null}
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="flex min-w-0 flex-col gap-1.5"><span className="text-[10.5px] font-medium text-text-muted">{label}</span>{children}</label>;
}
