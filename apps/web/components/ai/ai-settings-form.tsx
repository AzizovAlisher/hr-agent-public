"use client";

import { Loader2, Save, ShieldCheck } from "lucide-react";
import { useState, useTransition } from "react";

import { updateAiSettingsAction } from "@/app/ai/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getAiCopy } from "@/lib/ai/copy";
import type { AiOrganizationSettings } from "@/lib/ai/types";

export function AiSettingsForm({ initialSettings }: { initialSettings: AiOrganizationSettings }) {
  const { locale } = useI18n();
  const copy = getAiCopy(locale);
  const [settings, setSettings] = useState(initialSettings);
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [pending, startTransition] = useTransition();

  function save() {
    setNotice(null);
    startTransition(async () => {
      const result = await updateAiSettingsAction(settings);
      if (result.ok) {
        setSettings(result.data);
        setNotice({ kind: "success", text: copy.settingsForm.saved });
      } else {
        setNotice({ kind: "error", text: result.error || copy.settingsForm.failed });
      }
    });
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <label className="flex cursor-pointer items-start gap-3">
          <input type="checkbox" checked={settings.feature_enabled} onChange={(event) => setSettings((current) => ({ ...current, feature_enabled: event.target.checked }))} className="mt-0.5 h-4 w-4 accent-[var(--accent)]" />
          <span><span className="block text-[13px] font-medium">{copy.settingsForm.enabled}</span><span className="mt-1 block text-[11px] leading-5 text-text-muted">{copy.settingsForm.enabledHint}</span></span>
        </label>
      </section>

      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <h2 className="text-[12px] font-semibold">{locale === "ru" ? "Значения по умолчанию" : "Mission defaults"}</h2>
        <div className="mt-4 grid gap-4 sm:grid-cols-3">
          <NumberField label={copy.settingsForm.headcount} value={settings.default_headcount} min={1} max={100} onChange={(value) => setSettings((current) => ({ ...current, default_headcount: value }))} />
          <NumberField label={copy.settingsForm.shortlist} value={settings.default_shortlist_size} min={1} max={20} onChange={(value) => setSettings((current) => ({ ...current, default_shortlist_size: value }))} />
          <BudgetField
            label={copy.settingsForm.budget}
            hint={copy.settingsForm.budgetHint}
            locale={locale}
            value={settings.default_model_budget_cents}
            onChange={(value) =>
              setSettings((current) => ({
                ...current,
                default_model_budget_cents: value,
              }))
            }
          />
        </div>
      </section>

      <section className="rounded-xl border border-score-high/20 bg-score-high/5 p-5">
        <div className="flex items-start gap-3"><ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-score-high" /><div><h2 className="text-[12px] font-semibold text-score-high">{copy.settingsForm.boundaries}</h2><p className="mt-1 text-[11px] leading-5 text-text-muted">{copy.settingsForm.boundariesHint}</p><div className="mt-3 flex flex-wrap gap-1.5">{(locale === "ru" ? ["Найм", "Отказ", "Оффер", "Зарплата", "Экспорт", "Удаление"] : ["Hire", "Reject", "Offer", "Compensation", "Export", "Delete"]).map((label) => <span key={label} className="rounded-full border border-score-high/20 bg-background px-2 py-1 text-[9.5px] text-text-muted">{label}</span>)}</div></div></div>
      </section>

      <div className="flex items-center justify-between gap-3">
        <div>{notice ? <p className={notice.kind === "success" ? "text-[11px] text-score-high" : "text-[11px] text-score-low"}>{notice.text}</p> : null}</div>
        <Button onClick={save} disabled={pending}>{pending ? <Loader2 className="animate-spin" /> : <Save />}{copy.settingsForm.save}</Button>
      </div>
    </div>
  );
}

function NumberField({ label, hint, value, min, max, onChange }: { label: string; hint?: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  return <label className="block"><span className="mb-1.5 block text-[10.5px] font-medium text-text-muted">{label}</span><Input type="number" min={min} max={max} value={value} onChange={(event) => onChange(Math.max(min, Math.min(max, Number(event.target.value) || 0)))} />{hint ? <span className="mt-1 block text-[9px] leading-4 text-text-faint">{hint}</span> : null}</label>;
}

function BudgetField({ label, hint, locale, value, onChange }: { label: string; hint: string; locale: "ru" | "en"; value: number | null; onChange: (value: number | null) => void }) {
  const unlimited = value === null;
  return (
    <div>
      <span className="mb-1.5 block text-[10.5px] font-medium text-text-muted">{label}</span>
      <Input
        aria-label={label}
        type="number"
        min={0}
        max={1_000_000}
        disabled={unlimited}
        value={value ?? 0}
        onChange={(event) => onChange(Math.max(0, Math.min(1_000_000, Number(event.target.value) || 0)))}
      />
      <label className="mt-2 flex cursor-pointer items-center gap-2 text-[10px] text-text-muted">
        <input
          type="checkbox"
          checked={unlimited}
          onChange={(event) => onChange(event.target.checked ? null : 0)}
          className="h-3.5 w-3.5 accent-[var(--accent)]"
        />
        {locale === "ru" ? "Без лимита" : "Unlimited"}
      </label>
      <span className="mt-1 block text-[9px] leading-4 text-text-faint">{hint}</span>
    </div>
  );
}
