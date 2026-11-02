"use client";

import { useState } from "react";
import { toast } from "sonner";

import { createFolderAction, updateFolderAction } from "@/lib/actions";
import type { Folder } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useI18n } from "@/components/i18n/locale-provider";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  DEFAULT_FOLDER_COLOR,
  FOLDER_COLOR_NAMES,
  FOLDER_ICON_IDS,
  folderColor,
  folderIcon,
} from "@/components/positions/folder-ui";

/** @ui-only Folder-dialog composition properties. */
type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** edit an existing folder, or create a new one when null */
  folder: Folder | null;
  /** for create: the parent folder id (null = top-level) */
  parentId?: string | null;
  onSaved: () => void;
};

export function FolderDialog({
  open,
  onOpenChange,
  folder,
  parentId = null,
  onSaved,
}: Props) {
  const { m } = useI18n();
  const isEdit = folder != null;
  const [name, setName] = useState(folder?.name ?? "");
  const [color, setColor] = useState(folder?.color ?? DEFAULT_FOLDER_COLOR);
  const [icon, setIcon] = useState(folder?.icon ?? "folder");
  const [busy, setBusy] = useState(false);

  // Re-seed local state whenever the dialog opens for a different target.
  const key = `${folder?.id ?? "new"}:${open}`;
  const [lastKey, setLastKey] = useState(key);
  if (key !== lastKey) {
    setLastKey(key);
    setName(folder?.name ?? "");
    setColor(folder?.color ?? DEFAULT_FOLDER_COLOR);
    setIcon(folder?.icon ?? "folder");
  }

  async function handleSave() {
    const trimmed = name.trim();
    if (!trimmed) return;
    setBusy(true);
    const res = isEdit
      ? await updateFolderAction(folder.id, { name: trimmed, color, icon })
      : await createFolderAction({ name: trimmed, parent_id: parentId, color, icon });
    setBusy(false);
    if (!res.ok) {
      const msg = res.error.includes("duplicate_name")
        ? m.dashboard.folderDuplicate
        : res.error.includes("max_depth")
          ? m.dashboard.folderMaxDepth
          : m.dashboard.folderActionFailed(res.error);
      toast.error(msg);
      return;
    }
    onOpenChange(false);
    onSaved();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[420px]">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? m.dashboard.folderEditTitle : m.dashboard.folderCreateTitle}
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-1">
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSave();
            }}
            maxLength={64}
            placeholder={m.dashboard.folderNamePlaceholder}
          />

          <div>
            <p className="mb-1.5 text-[11px] uppercase tracking-wider text-text-faint">
              {m.dashboard.folderColorLabel}
            </p>
            <div className="flex flex-wrap gap-1.5">
              {FOLDER_COLOR_NAMES.map((c) => {
                const cls = folderColor(c);
                return (
                  <button
                    key={c}
                    type="button"
                    aria-label={c}
                    onClick={() => setColor(c)}
                    className={cn(
                      "h-6 w-6 rounded-full border-2 transition-transform",
                      cls.dot,
                      color === c
                        ? "scale-110 border-text"
                        : "border-transparent hover:scale-105",
                    )}
                  />
                );
              })}
            </div>
          </div>

          <div>
            <p className="mb-1.5 text-[11px] uppercase tracking-wider text-text-faint">
              {m.dashboard.folderIconLabel}
            </p>
            <div className="flex flex-wrap gap-1.5">
              {FOLDER_ICON_IDS.map((id) => {
                const Icon = folderIcon(id);
                const active = icon === id;
                const cls = folderColor(color);
                return (
                  <button
                    key={id}
                    type="button"
                    aria-label={id}
                    onClick={() => setIcon(id)}
                    className={cn(
                      "flex h-8 w-8 items-center justify-center rounded-[6px] border transition-colors",
                      active
                        ? cn(cls.bg, cls.border, cls.text)
                        : "border-border bg-bg-elevated text-text-muted hover:border-border-strong",
                    )}
                  >
                    <Icon className="h-4 w-4" />
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            {m.dashboard.folderCancel}
          </Button>
          <Button onClick={handleSave} disabled={busy || !name.trim()}>
            {isEdit ? m.dashboard.folderSave : m.dashboard.folderCreate}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
