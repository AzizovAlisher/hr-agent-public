"use client";

import { useEffect, useState } from "react";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import type { ApplicationDisposition } from "@/lib/types";

const DISPOSITIONS: ApplicationDisposition[] = [
  "not_qualified",
  "withdrew",
  "compensation",
  "timing",
  "duplicate",
  "position_closed",
  "other",
];

export function DispositionDialog({
  open,
  count = 1,
  busy = false,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  count?: number;
  busy?: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (disposition: ApplicationDisposition, reason: string) => void;
}) {
  const { m } = useI18n();
  const [disposition, setDisposition] =
    useState<ApplicationDisposition>("not_qualified");
  const [reason, setReason] = useState("");

  useEffect(() => {
    if (!open) {
      setDisposition("not_qualified");
      setReason("");
    }
  }, [open]);

  const cleanReason = reason.trim();

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{m.kanban.rejectTitle}</DialogTitle>
          <DialogDescription>{m.kanban.rejectDescription(count)}</DialogDescription>
        </DialogHeader>
        <label className="grid gap-1.5 text-[11px] font-medium text-text-muted">
          {m.kanban.dispositionLabel}
          <select
            autoFocus
            value={disposition}
            onChange={(event) =>
              setDisposition(event.target.value as ApplicationDisposition)
            }
            className="min-h-9 rounded-[6px] border border-border bg-bg-elevated px-3 text-[12px] text-text outline-none focus:border-border-strong"
          >
            {DISPOSITIONS.map((value) => (
              <option key={value} value={value}>
                {m.kanban.dispositions[value]}
              </option>
            ))}
          </select>
        </label>
        <label className="grid gap-1.5 text-[11px] font-medium text-text-muted">
          {m.kanban.reasonLabel}
          <Textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={m.kanban.reasonPlaceholder}
            maxLength={2000}
            rows={4}
            required
          />
        </label>
        <DialogFooter className="gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            {m.common.cancel}
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={!cleanReason || busy}
            onClick={() => onConfirm(disposition, cleanReason)}
          >
            {m.kanban.confirmReject}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
