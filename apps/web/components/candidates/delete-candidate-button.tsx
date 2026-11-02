"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";

import { deleteCandidateAction } from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/** @ui-only Delete-button visual variant. */
type Variant = "topbar" | "icon";

/**
 * Delete a candidate with a confirmation dialog. Two visual variants:
 *  - "topbar": full pill button used on the candidate detail page topbar
 *  - "icon":   compact trash icon used in row hover states on the list
 *
 * On success, navigates back to /candidates (topbar) or just refreshes the
 * list (icon — caller decides whether to redirect via redirectAfter).
 */
export function DeleteCandidateButton({
  candidateId,
  candidateLabel,
  variant = "topbar",
  redirectAfter = variant === "topbar" ? "/candidates" : null,
}: {
  candidateId: string;
  candidateLabel?: string;
  variant?: Variant;
  redirectAfter?: string | null;
}) {
  const { m } = useI18n();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [pending, start] = useTransition();

  function confirmDelete() {
    if (pending) return;
    start(async () => {
      const res = await deleteCandidateAction(candidateId);
      if (!res.ok) {
        toast.error(m.candidate.deleteFailedToast(res.error));
        return;
      }
      // Close dialog first so the redirect doesn't render the dialog over the
      // destination page mid-transition.
      setOpen(false);
      toast.success(m.candidate.deletedToast);
      if (redirectAfter) {
        // The server action already revalidatePath()'d /candidates, so this
        // push lands on a fresh list. We deliberately do NOT call
        // router.refresh() — that would re-render the current route (the
        // detail page of the just-deleted candidate) and flash a 404 page
        // before navigation completes.
        router.push(redirectAfter);
      } else {
        router.refresh();
      }
    });
  }

  return (
    <>
      {variant === "topbar" ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-score-low/10 hover:text-score-low"
        >
          <Trash2 className="h-3 w-3" />
          {m.candidate.delete}
        </button>
      ) : (
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setOpen(true);
          }}
          aria-label={m.candidate.delete}
          className="inline-flex h-6 w-6 items-center justify-center rounded-[4px] text-text-faint opacity-0 transition-all hover:bg-score-low/10 hover:text-score-low group-hover:opacity-100"
        >
          <Trash2 className="h-3 w-3" />
        </button>
      )}

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-[440px]">
          <DialogHeader>
            <DialogTitle className="text-[15px]">
              {m.candidate.confirmDelete}
            </DialogTitle>
            <DialogDescription className="text-[12.5px] text-text-muted">
              {m.candidate.confirmDeleteDesc(candidateLabel ?? "—")}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="mt-2 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-[6px] px-3 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
            >
              {m.common.cancel}
            </button>
            <button
              type="button"
              onClick={confirmDelete}
              disabled={pending}
              className="inline-flex items-center gap-1.5 rounded-[6px] bg-score-low px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              <Trash2 className="h-3 w-3" />
              {pending ? m.candidate.deleting : m.candidate.delete}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
