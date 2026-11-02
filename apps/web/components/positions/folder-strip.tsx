"use client";

import { useState } from "react";
import { useDroppable } from "@dnd-kit/core";
import { SortableContext, useSortable, horizontalListSortingStrategy } from "@dnd-kit/sortable";
import {
  ChevronRight,
  FolderPlus,
  MoreHorizontal,
  Pencil,
  Palette,
  Plus,
  Archive,
  ArchiveRestore,
  ArrowLeft,
  ArrowRight,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import type { Folder } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useI18n } from "@/components/i18n/locale-provider";
import { deleteFolderAction, updateFolderAction } from "@/lib/actions";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { FolderDialog } from "@/components/positions/folder-dialog";
import { folderColor, folderIcon } from "@/components/positions/folder-ui";

/** @ui-only Folder-filter selection including the synthetic unfiled value. */
export type FolderSelection = string | null | "__none__";

/** @ui-only Folder-strip composition properties. */
type Props = {
  tree: Folder[];
  currentParentId: string | null;
  active: FolderSelection;
  totalCount: number;
  unsortedCount: number;
  onSelect: (sel: FolderSelection) => void;
  onDrill: (parentId: string) => void;
  onAscend: () => void;
  onChanged: () => void;
  onReorder: (folderId: string, direction: -1 | 1) => void;
  canManage: boolean;
};

export function FolderStrip({
  tree,
  currentParentId,
  active,
  totalCount,
  unsortedCount,
  onSelect,
  onDrill,
  onAscend,
  onChanged,
  onReorder,
  canManage,
}: Props) {
  const { m } = useI18n();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<Folder | null>(null);
  const [createParent, setCreateParent] = useState<string | null>(null);

  const parent =
    currentParentId != null ? tree.find((f) => f.id === currentParentId) ?? null : null;
  const items = parent ? parent.children ?? [] : tree;
  const itemIds = items.map((f) => f.id);

  function openCreate(parentId: string | null) {
    setEditing(null);
    setCreateParent(parentId);
    setDialogOpen(true);
  }
  function openEdit(folder: Folder) {
    setEditing(folder);
    setDialogOpen(true);
  }

  async function toggleArchive(folder: Folder) {
    const res = await updateFolderAction(folder.id, { is_archived: !folder.is_archived });
    if (!res.ok) toast.error(m.dashboard.folderActionFailed(res.error));
    else onChanged();
  }

  async function handleDelete(folder: Folder) {
    const childCount = folder.children?.length ?? 0;
    // descendant_count already = sum of children's position_count (2-level tree),
    // so it covers child-folder positions; don't add them a second time.
    const total = folder.position_count + folder.descendant_count;
    if (!window.confirm(m.dashboard.folderDeleteConfirm(folder.name, total))) return;
    const res = await deleteFolderAction(folder.id, { force: childCount > 0 });
    if (!res.ok) toast.error(m.dashboard.folderActionFailed(res.error));
    else {
      if (active === folder.id) onSelect(currentParentId ?? null);
      onChanged();
    }
  }

  return (
    <div className="mb-3 flex flex-wrap items-center gap-1.5">
      {/* breadcrumb */}
      {parent ? (
        <div className="mr-1 flex items-center gap-1 text-[11.5px]">
          <button
            type="button"
            onClick={onAscend}
            className="text-text-muted hover:text-text"
          >
            {m.dashboard.folderBack}
          </button>
          <ChevronRight className="h-3 w-3 text-text-faint" />
          <span className="font-medium text-text">{parent.name}</span>
        </div>
      ) : (
        <>
          <AllPill
            label={m.dashboard.folderAll}
            count={totalCount}
            active={active === null}
            onClick={() => onSelect(null)}
          />
          <UnsortedDrop
            label={m.dashboard.folderUnsorted}
            count={unsortedCount}
            active={active === "__none__"}
            onClick={() => onSelect("__none__")}
            canManage={canManage}
          />
        </>
      )}

      {parent ? (
        <AllPill
          label={`${m.dashboard.folderAll} · ${parent.name}`}
          count={parent.position_count + parent.descendant_count}
          active={active === parent.id}
          onClick={() => onSelect(parent.id)}
        />
      ) : null}

      <SortableContext items={itemIds} strategy={horizontalListSortingStrategy}>
        {items.map((f, index) => (
          <FolderPill
            key={f.id}
            folder={f}
            active={active === f.id}
            isParent={!parent && (f.children?.length ?? 0) > 0}
            onSelect={() => {
              if (!parent && (f.children?.length ?? 0) > 0) {
                onDrill(f.id);
                onSelect(f.id);
              } else {
                onSelect(f.id);
              }
            }}
            onAddSub={() => openCreate(f.id)}
            onEdit={() => openEdit(f)}
            onArchive={() => toggleArchive(f)}
            onDelete={() => handleDelete(f)}
            onMoveLeft={() => onReorder(f.id, -1)}
            onMoveRight={() => onReorder(f.id, 1)}
            canMoveLeft={index > 0}
            canMoveRight={index < items.length - 1}
            canManage={canManage}
          />
        ))}
      </SortableContext>

      {canManage ? (
        <button
          type="button"
          onClick={() => openCreate(parent ? parent.id : null)}
          className="press inline-flex items-center gap-1 rounded-full border border-dashed border-border px-2.5 py-[3px] text-[11.5px] text-text-muted transition-colors hover:border-border-strong hover:text-text"
        >
          {parent ? <FolderPlus className="h-3 w-3" /> : <Plus className="h-3 w-3" />}
          {parent ? m.dashboard.folderNewSub : m.dashboard.folderNew}
        </button>
      ) : null}

      <FolderDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        folder={editing}
        parentId={createParent}
        onSaved={onChanged}
      />
    </div>
  );
}

function AllPill({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full border px-2.5 py-[3px] text-[11.5px] font-medium transition-all",
        active
          ? "border-accent-border bg-accent-muted text-primary"
          : "border-border bg-bg-elevated text-text-muted hover:border-border-strong hover:text-text",
      )}
    >
      {label}
      <span className="ml-1.5 font-mono text-[10.5px] tabular-nums">· {count}</span>
    </button>
  );
}

function UnsortedDrop(props: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  canManage: boolean;
}) {
  // Droppable so a dragged card can be moved to Unsorted (folder_id = null).
  const { setNodeRef, isOver } = useDroppable({
    id: "drop-none",
    disabled: !props.canManage,
  });
  return (
    <button
      ref={setNodeRef}
      type="button"
      onClick={props.onClick}
      className={cn(
        "rounded-full border px-2.5 py-[3px] text-[11.5px] font-medium transition-all",
        isOver
          ? "border-accent-border bg-accent-muted text-primary ring-1 ring-accent-border"
          : props.active
            ? "border-accent-border bg-accent-muted text-primary"
            : "border-border bg-bg-elevated text-text-muted hover:border-border-strong hover:text-text",
      )}
    >
      {props.label}
      <span className="ml-1.5 font-mono text-[10.5px] tabular-nums">
        · {props.count}
      </span>
    </button>
  );
}

function FolderPill({
  folder,
  active,
  isParent,
  onSelect,
  onAddSub,
  onEdit,
  onArchive,
  onDelete,
  onMoveLeft,
  onMoveRight,
  canMoveLeft,
  canMoveRight,
  canManage,
}: {
  folder: Folder;
  active: boolean;
  isParent: boolean;
  onSelect: () => void;
  onAddSub: () => void;
  onEdit: () => void;
  onArchive: () => void;
  onDelete: () => void;
  onMoveLeft: () => void;
  onMoveRight: () => void;
  canMoveLeft: boolean;
  canMoveRight: boolean;
  canManage: boolean;
}) {
  const { m } = useI18n();
  const {
    setNodeRef,
    attributes,
    listeners,
    transform,
    transition,
    isDragging,
    isOver,
  } = useSortable({ id: folder.id, disabled: !canManage });
  const cls = folderColor(folder.color);
  const Icon = folderIcon(folder.icon);
  const total = folder.position_count + folder.descendant_count;

  const style = {
    transform: transform
      ? `translate3d(${transform.x}px, ${transform.y}px, 0)`
      : undefined,
    transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={cn(
        "group inline-flex items-center rounded-full border text-[11.5px] font-medium transition-all",
        active ? cn(cls.bg, cls.border, cls.text) : cn("bg-bg-elevated", cls.border, "text-text-muted"),
        isOver && "ring-1 ring-accent-border",
        isDragging && "opacity-40",
      )}
    >
      <button
        type="button"
        {...attributes}
        {...listeners}
        onClick={onSelect}
        className={cn(
          "flex items-center gap-1.5 py-[3px] pl-2.5 pr-1",
          canManage && "touch-none",
        )}
      >
        <Icon className={cn("h-3 w-3", cls.text)} />
        <span>{folder.name}</span>
        <span className="font-mono text-[10.5px] tabular-nums">· {total}</span>
        {isParent ? <ChevronRight className="h-3 w-3 opacity-60" /> : null}
      </button>

      {canManage ? <DropdownMenu>
        <DropdownMenuTrigger
          aria-label={m.dashboard.folderActions}
          onPointerDown={(e) => e.stopPropagation()}
          className="flex items-center px-1.5 py-[3px] text-text-faint opacity-0 transition-opacity hover:text-text group-hover:opacity-100 data-[state=open]:opacity-100"
        >
          <MoreHorizontal className="h-3.5 w-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-44">
          <DropdownMenuItem onClick={onEdit}>
            <Pencil className="h-3.5 w-3.5" /> {m.dashboard.folderRename}
          </DropdownMenuItem>
          <DropdownMenuItem onClick={onEdit}>
            <Palette className="h-3.5 w-3.5" /> {m.dashboard.folderColorIcon}
          </DropdownMenuItem>
          {folder.parent_id == null ? (
            <DropdownMenuItem onClick={onAddSub}>
              <FolderPlus className="h-3.5 w-3.5" /> {m.dashboard.folderNewSub}
            </DropdownMenuItem>
          ) : null}
          <DropdownMenuItem disabled={!canMoveLeft} onClick={onMoveLeft}>
            <ArrowLeft className="h-3.5 w-3.5" /> {m.dashboard.folderMoveLeft}
          </DropdownMenuItem>
          <DropdownMenuItem disabled={!canMoveRight} onClick={onMoveRight}>
            <ArrowRight className="h-3.5 w-3.5" /> {m.dashboard.folderMoveRight}
          </DropdownMenuItem>
          <DropdownMenuItem onClick={onArchive}>
            {folder.is_archived ? (
              <>
                <ArchiveRestore className="h-3.5 w-3.5" /> {m.dashboard.folderUnarchive}
              </>
            ) : (
              <>
                <Archive className="h-3.5 w-3.5" /> {m.dashboard.folderArchive}
              </>
            )}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={onDelete} className="text-score-low">
            <Trash2 className="h-3.5 w-3.5" /> {m.dashboard.folderDelete}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu> : null}
    </div>
  );
}
