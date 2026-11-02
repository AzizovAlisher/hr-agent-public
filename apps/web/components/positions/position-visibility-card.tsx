"use client";

import { useEffect, useState, useTransition } from "react";
import { AlertTriangle, Check, Eye, Lock, ShieldAlert, X } from "lucide-react";
import { toast } from "sonner";

import { updatePositionAction } from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import type { PositionSensitivity } from "@/lib/types";
import { cn } from "@/lib/utils";

/** @ui-only Position-visibility card properties. */
type Props = {
  positionId: string;
  initialSensitivity: PositionSensitivity;
  initialPublicDescription: string | null;
  canEdit: boolean;
};

const TIERS: PositionSensitivity[] = ["public", "nda", "restricted"];

const TIER_ICON = {
  public: Eye,
  nda: Lock,
  restricted: ShieldAlert,
} as const;

/**
 * Controls how much of this role an unauthenticated visitor may see, and holds
 * the vetted public stand-in text used when it is confidential.
 *
 * The tiers are enforced server-side (see app/services/public_positions.py) —
 * nothing here is presentational-only hiding. This card just makes the current
 * policy legible and editable.
 */
export function PositionVisibilityCard({
  positionId,
  initialSensitivity,
  initialPublicDescription,
  canEdit,
}: Props) {
  const { m } = useI18n();
  const [sensitivity, setSensitivity] =
    useState<PositionSensitivity>(initialSensitivity);
  const [pending, startSave] = useTransition();

  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(initialPublicDescription ?? "");
  const [saved, setSaved] = useState(initialPublicDescription ?? "");
  const [savingText, startSaveText] = useTransition();

  // Re-sync from the server whenever the underlying record changes. Generating
  // a profile rewrites public_description server-side and revalidates this
  // page; without this the card keeps showing the pre-generation text, and
  // saving from that stale state would re-publish the LESS redacted version.
  // Skipped while the user is mid-edit so a background revalidation can't
  // discard what they are typing.
  useEffect(() => {
    if (editing) return;
    const next = initialPublicDescription ?? "";
    setSaved(next);
    setValue(next);
  }, [initialPublicDescription, editing]);

  useEffect(() => {
    setSensitivity(initialSensitivity);
  }, [initialSensitivity]);

  const confidential = sensitivity !== "public";
  const missingPublicText = confidential && !saved.trim();

  function changeTier(next: PositionSensitivity) {
    if (next === sensitivity) return;
    const previous = sensitivity;
    setSensitivity(next);
    startSave(async () => {
      const res = await updatePositionAction(positionId, { sensitivity: next });
      if (res.ok) {
        toast.success(m.position.sensitivityChangedToast(m.position.tier[next]));
      } else {
        setSensitivity(previous);
        toast.error(m.position.sensitivityChangeFailedToast(res.error));
      }
    });
  }

  function savePublicText() {
    startSaveText(async () => {
      const next = value.trim();
      const res = await updatePositionAction(positionId, {
        public_description: next,
      });
      if (res.ok) {
        setSaved(next);
        setEditing(false);
        toast.success(m.position.publicDescriptionSavedToast);
      } else {
        toast.error(m.position.profileSaveFailedToast(res.error));
      }
    });
  }

  return (
    <div className="rounded-lg border border-border bg-bg-elevated">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-3">
        <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {m.position.visibilityLabel}
        </span>
        <div className="flex flex-wrap gap-1">
          {(canEdit ? TIERS : [sensitivity]).map((tier) => {
            const Icon = TIER_ICON[tier];
            const active = sensitivity === tier;
            return canEdit ? (
              <button
                key={tier}
                type="button"
                onClick={() => changeTier(tier)}
                disabled={pending}
                aria-pressed={active}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[3px] text-[11.5px] font-medium transition-colors disabled:opacity-50",
                  active
                    ? "border-border-strong bg-bg text-text"
                    : "border-border bg-transparent text-text-faint hover:border-border-strong hover:text-text",
                )}
              >
                <Icon className="h-3 w-3" />
                {m.position.tier[tier]}
              </button>
            ) : (
              <span
                key={tier}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[3px] text-[11.5px] font-medium",
                  "border-border-strong bg-bg text-text",
                )}
              >
                <Icon className="h-3 w-3" />
                {m.position.tier[tier]}
              </span>
            );
          })}
        </div>
      </div>

      <p className="px-4 pb-3 pt-1.5 text-[12px] leading-[1.55] text-text-muted">
        {m.position.tierHint[sensitivity]}
      </p>

      {confidential ? (
        <div className="border-t border-border px-4 py-3">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.position.publicDescriptionLabel}
            </span>
            {canEdit && !editing ? (
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="rounded-[6px] px-2 py-0.5 text-[11px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
              >
                {m.common.edit}
              </button>
            ) : null}
          </div>

          {editing && canEdit ? (
            <>
              <textarea
                autoFocus
                value={value}
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    savePublicText();
                  } else if (e.key === "Escape") {
                    setValue(saved);
                    setEditing(false);
                  }
                }}
                rows={3}
                placeholder={m.position.publicDescriptionPlaceholder}
                className="block min-h-[72px] w-full resize-y rounded-[6px] border border-border bg-bg p-2.5 text-[12.5px] leading-[1.5] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
              />
              <div className="mt-2 flex justify-end gap-1.5">
                <button
                  type="button"
                  onClick={() => {
                    setValue(saved);
                    setEditing(false);
                  }}
                  className="inline-flex items-center gap-1 rounded-[6px] px-2 py-1 text-[11px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
                >
                  <X className="h-3 w-3" />
                  {m.common.cancel}
                </button>
                <button
                  type="button"
                  onClick={savePublicText}
                  disabled={savingText}
                  className="inline-flex items-center gap-1 rounded-[6px] bg-text px-2 py-1 text-[11px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
                >
                  <Check className="h-3 w-3" />
                  {savingText ? m.common.saving : m.common.save}
                </button>
              </div>
            </>
          ) : missingPublicText ? (
            <p className="flex items-start gap-2 rounded-[6px] border border-dashed border-border px-2.5 py-2 text-[12px] leading-[1.5] text-text-faint">
              <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>{m.position.publicDescriptionEmpty}</span>
            </p>
          ) : (
            <p className="whitespace-pre-line text-[12.5px] leading-[1.6] text-text">
              {saved}
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}
