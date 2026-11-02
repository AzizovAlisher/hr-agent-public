"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState, useTransition } from "react";
import {
  ChevronDown,
  CircleDot,
  EyeOff,
  Globe2,
  Pause,
  Pencil,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";

import {
  deletePositionAction,
  rescoreAllAction,
  updatePositionAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "sonner";

/** @ui-only Position topbar-action composition properties. */
type Props = {
  positionId: string;
  status: string;
  publicApplyEnabled: boolean;
  canRescore: boolean;
};

export function PositionTopbarActions({
  positionId,
  status,
  publicApplyEnabled,
  canRescore,
}: Props) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { m } = useI18n();
  const [statusPending, startStatus] = useTransition();
  const [publicationPending, startPublication] = useTransition();
  const [deleting, startDelete] = useTransition();
  const [rescoring, startRescore] = useTransition();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const isEditing = searchParams.get("edit") === "1";

  function toggleEdit() {
    const next = isEditing ? pathname : `${pathname}?edit=1`;
    router.replace(next, { scroll: false });
  }

  function changeStatus(newStatus: string) {
    startStatus(async () => {
      const res = await updatePositionAction(positionId, { status: newStatus });
      if (res.ok) {
        toast.success(m.position.actionsMenu.statusChangedToast);
        router.refresh();
      } else {
        toast.error(m.position.saveFailedToast(res.error));
      }
    });
  }

  function doRescore() {
    startRescore(async () => {
      const res = await rescoreAllAction(positionId);
      if (!res.ok) {
        toast.error(m.position.saveFailedToast(res.error));
        return;
      }
      if (res.data.queued === 0) {
        toast.info(m.position.actionsMenu.rescoreEmptyToast);
        return;
      }
      toast.success(m.position.actionsMenu.rescoreQueuedToast(res.data.queued));
    });
  }

  function changePublication(enabled: boolean) {
    startPublication(async () => {
      const res = await updatePositionAction(positionId, {
        public_apply_enabled: enabled,
      });
      if (res.ok) {
        toast.success(
          enabled
            ? m.position.actionsMenu.publishedToast
            : m.position.actionsMenu.unpublishedToast,
        );
        router.refresh();
      } else {
        toast.error(m.position.saveFailedToast(res.error));
      }
    });
  }

  function doDelete() {
    startDelete(async () => {
      const res = await deletePositionAction(positionId);
      if (res.ok) {
        toast.success(m.position.actionsMenu.deletedToast);
        router.push("/dashboard");
      } else {
        toast.error(m.position.saveFailedToast(res.error));
      }
    });
  }

  return (
    <>
      <button
        type="button"
        onClick={toggleEdit}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-1.5 text-[12px] font-medium transition-colors",
          isEditing
            ? "bg-bg-active text-text"
            : "text-text-muted hover:bg-bg-hover hover:text-text",
        )}
      >
        {isEditing ? (
          <X className="h-3 w-3" />
        ) : (
          <Pencil className="h-3 w-3" />
        )}
        {isEditing ? m.position.cancel : m.position.edit}
      </button>

      <DropdownMenu>
        <DropdownMenuTrigger
          disabled={statusPending || publicationPending || deleting}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-2.5 py-1.5 text-[12px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
            (statusPending || publicationPending || deleting) && "opacity-50",
          )}
        >
          {m.position.actions}
          <ChevronDown className="h-3 w-3" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-52" sideOffset={6}>
          {status !== "open" ? (
            <DropdownMenuItem
              className="text-xs"
              onClick={() => changeStatus("open")}
            >
              <CircleDot className="mr-2 h-3.5 w-3.5 text-primary" />
              {m.position.actionsMenu.reopen}
            </DropdownMenuItem>
          ) : null}
          {status !== "paused" ? (
            <DropdownMenuItem
              className="text-xs"
              onClick={() => changeStatus("paused")}
            >
              <Pause className="mr-2 h-3.5 w-3.5" />
              {m.position.actionsMenu.pause}
            </DropdownMenuItem>
          ) : null}
          {status !== "closed" ? (
            <DropdownMenuItem
              className="text-xs"
              onClick={() => changeStatus("closed")}
            >
              <X className="mr-2 h-3.5 w-3.5" />
              {m.position.actionsMenu.close}
            </DropdownMenuItem>
          ) : null}
          <DropdownMenuSeparator />
          {publicApplyEnabled ? (
            <DropdownMenuItem
              className="text-xs"
              onClick={() => changePublication(false)}
            >
              <EyeOff className="mr-2 h-3.5 w-3.5" />
              {m.position.actionsMenu.unpublish}
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem
              className="text-xs"
              onClick={() => changePublication(true)}
            >
              <Globe2 className="mr-2 h-3.5 w-3.5 text-primary" />
              {m.position.actionsMenu.publish}
            </DropdownMenuItem>
          )}
          {canRescore ? (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="text-xs"
                disabled={rescoring}
                onClick={doRescore}
              >
                <RefreshCw
                  className={cn(
                    "mr-2 h-3.5 w-3.5",
                    rescoring && "animate-spin",
                  )}
                />
                {m.position.actionsMenu.rescoreAll}
              </DropdownMenuItem>
            </>
          ) : null}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="text-xs text-score-low focus:text-score-low"
            closeOnClick={false}
            onClick={() => setConfirmOpen(true)}
          >
            <Trash2 className="mr-2 h-3.5 w-3.5" />
            {m.position.actionsMenu.delete}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="text-[15px]">
              {m.position.actionsMenu.confirmDelete}
            </DialogTitle>
            <DialogDescription className="text-[12px] text-text-muted">
              {m.position.actionsMenu.confirmDeleteDesc}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <button
              type="button"
              onClick={() => setConfirmOpen(false)}
              className="rounded-[6px] px-3 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
            >
              {m.position.cancel}
            </button>
            <button
              type="button"
              onClick={doDelete}
              disabled={deleting}
              className="inline-flex items-center gap-1.5 rounded-[6px] px-3 py-1.5 text-[12px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
              style={{ background: "var(--score-low)" }}
            >
              <Trash2 className="h-3.5 w-3.5" />
              {m.position.actionsMenu.delete}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
