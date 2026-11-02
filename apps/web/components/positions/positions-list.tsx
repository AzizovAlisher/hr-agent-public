"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  pointerWithin,
  useDraggable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import type { DragEndEvent, DragStartEvent } from "@dnd-kit/core";
import { arrayMove } from "@dnd-kit/sortable";
import { Check, FolderInput, GripVertical, ListFilter, MoreHorizontal, Search } from "lucide-react";
import { toast } from "sonner";

import type { Folder, Position } from "@/lib/types";
import { cn } from "@/lib/utils";
import { movePositionsAction, reorderFoldersAction } from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { PositionCard } from "@/components/positions/position-card";
import {
  FolderStrip,
  type FolderSelection,
} from "@/components/positions/folder-strip";
import { EmptyState } from "@/components/ui/empty-state";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/** @ui-only Position-list status filter. */
type Filter = "all" | "open" | "paused" | "closed";
/** @ui-only Position-list sort selection. */
type Sort = "recent" | "oldest" | "candidates" | "chats" | "title";

export function PositionsList({
  positions: allPositions,
  folders = [],
  canManage,
  initialFolderId = null,
}: {
  positions: Position[];
  folders?: Folder[];
  canManage: boolean;
  initialFolderId?: string | null;
}) {
  const { m } = useI18n();
  const router = useRouter();

  // Positions filed under a folder whose name starts with "_" are archived and
  // hidden from the dashboard. They stay in the API/DB — reversible, not a
  // delete. (Belt-and-braces with the server's exclude_archived.)
  const positions = useMemo(
    () => allPositions.filter((p) => !p.folder?.startsWith("_")),
    [allPositions],
  );

  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("recent");

  // Folder drill-down state. `active` is the current filter selection:
  // null = all, "__none__" = Unsorted, or a folder id (parent shows its whole
  // subtree, leaf shows itself). `currentParentId` is the level shown in the
  // strip (null = roots).
  const [currentParentId, setCurrentParentId] = useState<string | null>(null);
  const [active, setActive] = useState<FolderSelection>(null);
  const [folderStateRestored, setFolderStateRestored] = useState(false);
  const [dragId, setDragId] = useState<string | null>(null);

  const folderById = useMemo(() => {
    const map = new Map<string, Folder>();
    const walk = (fs: Folder[]) => {
      for (const f of fs) {
        map.set(f.id, f);
        if (f.children?.length) walk(f.children);
      }
    };
    walk(folders);
    return map;
  }, [folders]);

  // The selected folder is carried in the URL. This makes the project-create
  // redirect executable without leaving tenant-specific IDs in unscoped
  // browser storage, and it also gives refresh/back/forward stable behavior.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      // Remove browser keys used by older builds. Never import them because
      // they were not tied to an authenticated organization/user identity.
      window.localStorage.removeItem("dashboard.folder");
      window.localStorage.removeItem("dashboard.folder_id");
    } catch {
      /* localStorage disabled; URL selection remains authoritative */
    }
    if (initialFolderId === "__none__") {
      setActive("__none__");
    } else if (initialFolderId) {
      const f = folderById.get(initialFolderId);
      if (f?.parent_id != null) {
        setCurrentParentId(f.parent_id);
        setActive(initialFolderId);
      } else if (f?.children?.length) {
        setCurrentParentId(initialFolderId);
        setActive(initialFolderId);
      } else if (f) {
        setActive(initialFolderId);
      }
    }
    setFolderStateRestored(true);
  }, [folderById, initialFolderId]);

  useEffect(() => {
    if (typeof window === "undefined" || !folderStateRestored) return;
    const url = new URL(window.location.href);
    if (active === null) url.searchParams.delete("folder_id");
    else url.searchParams.set("folder_id", active);
    window.history.replaceState(
      window.history.state,
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, [active, folderStateRestored]);

  const childIdsOf = (parentId: string): Set<string> => {
    const f = folderById.get(parentId);
    const ids = new Set<string>([parentId]);
    f?.children?.forEach((c) => ids.add(c.id));
    return ids;
  };

  const inFolder = useMemo(() => {
    if (active === null) return positions;
    if (active === "__none__") return positions.filter((p) => !p.folder_id);
    const f = folderById.get(active);
    if (f && (f.children?.length ?? 0) > 0) {
      const ids = childIdsOf(active);
      return positions.filter((p) => p.folder_id && ids.has(p.folder_id));
    }
    return positions.filter((p) => p.folder_id === active);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [positions, active, folderById]);

  const unsortedCount = useMemo(
    () => positions.filter((p) => !p.folder_id).length,
    [positions],
  );

  const counts = useMemo(() => {
    const c = { all: inFolder.length, open: 0, paused: 0, closed: 0 };
    for (const p of inFolder) {
      if (p.status === "open") c.open += 1;
      else if (p.status === "paused") c.paused += 1;
      else if (p.status === "closed") c.closed += 1;
    }
    return c;
  }, [inFolder]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matched = inFolder.filter((p) => {
      if (filter !== "all" && p.status !== filter) return false;
      if (!q) return true;
      return (
        p.title.toLowerCase().includes(q) ||
        (p.description ?? "").toLowerCase().includes(q)
      );
    });
    const cmp = (a: Position, b: Position): number => {
      switch (sort) {
        case "recent":
          return Date.parse(b.updated_at) - Date.parse(a.updated_at);
        case "oldest":
          return Date.parse(a.updated_at) - Date.parse(b.updated_at);
        case "candidates":
          return b.candidate_count - a.candidate_count;
        case "chats":
          return (b.chat_session_count ?? 0) - (a.chat_session_count ?? 0);
        case "title":
          return a.title.localeCompare(b.title);
      }
    };
    return [...matched].sort(cmp);
  }, [inFolder, filter, query, sort]);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
  );

  async function handleDragEnd(e: DragEndEvent) {
    setDragId(null);
    if (!canManage) return;
    const { active: a, over } = e;
    if (!over) return;
    const aid = String(a.id);
    const oid = String(over.id);

    // Card → folder (move position).
    if (aid.startsWith("pos:")) {
      const posId = aid.slice(4);
      let target: string | null;
      if (oid === "drop-none") target = null;
      else if (folderById.has(oid)) target = oid;
      else return;
      await movePosition(posId, target);
      return;
    }

    // Folder → folder (reorder within the same level).
    const af = folderById.get(aid);
    const of = folderById.get(oid);
    if (!af || !of || aid === oid) return;
    const aLevel = af.parent_id ?? null;
    const oLevel = of.parent_id ?? null;
    if (aLevel !== oLevel) return;
    const siblings =
      aLevel == null
        ? folders.map((f) => f.id)
        : (folderById.get(aLevel)?.children ?? []).map((f) => f.id);
    const from = siblings.indexOf(aid);
    const to = siblings.indexOf(oid);
    if (from < 0 || to < 0) return;
    const reordered = arrayMove(siblings, from, to);
    const res = await reorderFoldersAction(aLevel, reordered);
    if (!res.ok) {
      toast.error(m.dashboard.folderActionFailed(res.error));
      return;
    }
    router.refresh();
  }

  async function movePosition(positionId: string, target: string | null) {
    const current = positions.find((position) => position.id === positionId);
    if (!current || current.folder_id === target) return;
    const res = await movePositionsAction([positionId], target);
    if (!res.ok) {
      toast.error(m.dashboard.folderActionFailed(res.error));
      return;
    }
    toast.success(m.dashboard.folderMovedToast(1));
    router.refresh();
  }

  async function reorderFolder(folderId: string, direction: -1 | 1) {
    const folder = folderById.get(folderId);
    if (!folder) return;
    const parentId = folder.parent_id ?? null;
    const siblings =
      parentId == null
        ? folders.map((item) => item.id)
        : (folderById.get(parentId)?.children ?? []).map((item) => item.id);
    const from = siblings.indexOf(folderId);
    const to = from + direction;
    if (from < 0 || to < 0 || to >= siblings.length) return;
    const res = await reorderFoldersAction(parentId, arrayMove(siblings, from, to));
    if (!res.ok) {
      toast.error(m.dashboard.folderActionFailed(res.error));
      return;
    }
    router.refresh();
  }

  const folderOptions = useMemo(
    () => [
      { id: null, label: m.dashboard.folderUnsorted },
      ...folders.flatMap((folder) => [
        { id: folder.id, label: folder.name },
        ...(folder.children ?? []).map((child) => ({
          id: child.id,
          label: `${folder.name} / ${child.name}`,
        })),
      ]),
    ],
    [folders, m.dashboard.folderUnsorted],
  );

  const dragTitle =
    dragId && dragId.startsWith("pos:")
      ? positions.find((p) => p.id === dragId.slice(4))?.title
      : null;

  const showStrip = folders.length > 0;

  return (
    <DndContext
      id="dashboard-positions-dnd"
      sensors={sensors}
      collisionDetection={pointerWithin}
      onDragStart={(e: DragStartEvent) => setDragId(String(e.active.id))}
      onDragEnd={handleDragEnd}
    >
      {showStrip ? (
        <FolderStrip
          tree={folders}
          currentParentId={currentParentId}
          active={active}
          totalCount={positions.length}
          unsortedCount={unsortedCount}
          onSelect={setActive}
          onDrill={(id) => setCurrentParentId(id)}
          onAscend={() => {
            setCurrentParentId(null);
            setActive(null);
          }}
          onChanged={() => router.refresh()}
          onReorder={(folderId, direction) => {
            void reorderFolder(folderId, direction);
          }}
          canManage={canManage}
        />
      ) : null}

      <div className="mb-5 flex flex-wrap items-center gap-2 border-b border-border pb-4">
        <div className="relative max-w-[320px] flex-1">
          <Search className="pointer-events-none absolute left-[11px] top-1/2 h-3 w-3 -translate-y-1/2 text-text-faint" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={m.dashboard.searchPlaceholder}
            className="w-full rounded-[6px] border border-border bg-bg-elevated py-[7px] pl-8 pr-3 text-[12px] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
          />
        </div>
        <FilterPill
          label={m.dashboard.filterAll}
          count={counts.all}
          active={filter === "all"}
          onClick={() => setFilter("all")}
          showCount={false}
        />
        <FilterPill
          label={m.dashboard.filterOpen}
          count={counts.open}
          active={filter === "open"}
          onClick={() => setFilter("open")}
        />
        <FilterPill
          label={m.dashboard.filterPaused}
          count={counts.paused}
          active={filter === "paused"}
          onClick={() => setFilter("paused")}
        />
        <FilterPill
          label={m.dashboard.filterClosed}
          count={counts.closed}
          active={filter === "closed"}
          onClick={() => setFilter("closed")}
        />
        <SortDropdown sort={sort} onChange={setSort} />
      </div>

      {filtered.length === 0 ? (
        <EmptyState
          title={
            query || filter !== "all"
              ? m.dashboard.noMatchesTitle
              : active !== null
                ? m.dashboard.folderEmptyTitle
                : m.dashboard.emptyTitle
          }
          description={
            query || filter !== "all"
              ? m.dashboard.noMatchesDesc
              : active !== null
                ? m.dashboard.folderEmptyDesc
                : m.dashboard.emptyDesc
          }
        />
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-[repeat(auto-fill,minmax(320px,1fr))]">
          {filtered.map((p, i) => (
            <DraggablePositionCard
              key={p.id}
              position={p}
              index={i}
              canManage={canManage}
              folderOptions={folderOptions}
              onMove={(target) => {
                void movePosition(p.id, target);
              }}
            />
          ))}
        </div>
      )}

      <DragOverlay dropAnimation={null}>
        {dragTitle ? (
          <div className="max-w-[280px] truncate rounded-[8px] border border-border-strong bg-bg-elevated px-3 py-2 text-[12.5px] font-medium shadow-lg ring-1 ring-accent-border">
            {dragTitle}
          </div>
        ) : null}
      </DragOverlay>
    </DndContext>
  );
}

function DraggablePositionCard({
  position,
  index,
  canManage,
  folderOptions,
  onMove,
}: {
  position: Position;
  index: number;
  canManage: boolean;
  folderOptions: Array<{ id: string | null; label: string }>;
  onMove: (target: string | null) => void;
}) {
  const { m } = useI18n();
  const { setNodeRef, listeners, attributes, isDragging } = useDraggable({
    id: `pos:${position.id}`,
    disabled: !canManage,
  });
  return (
    <div
      ref={setNodeRef}
      className={cn("reveal group/drag relative h-full", isDragging && "opacity-40")}
      style={{ animationDelay: `${Math.min(index, 8) * 40}ms` }}
    >
      {/* Left-edge drag handle — appears on hover; the card body stays a link. */}
      {canManage ? (
        <button
          {...listeners}
          {...attributes}
          aria-label={m.dashboard.dragPosition}
          className="absolute left-0 top-0 z-20 flex h-full w-5 cursor-grab touch-none items-center justify-center rounded-l-lg text-text-faint opacity-0 transition-opacity hover:bg-bg-hover focus-visible:opacity-100 group-hover/drag:opacity-100"
        >
          <GripVertical className="h-3.5 w-3.5" />
        </button>
      ) : null}
      {canManage ? (
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={m.dashboard.movePositionToFolder}
            onPointerDown={(event) => event.stopPropagation()}
            className="absolute bottom-3 right-3 z-20 flex h-7 w-7 items-center justify-center rounded-md border border-border bg-bg-elevated text-text-muted opacity-0 shadow-sm transition-opacity hover:bg-bg-hover focus-visible:opacity-100 group-hover/drag:opacity-100 data-[state=open]:opacity-100"
          >
            <MoreHorizontal className="h-4 w-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel>{m.dashboard.movePositionToFolder}</DropdownMenuLabel>
            {folderOptions.map((folder) => (
              <DropdownMenuItem
                key={folder.id ?? "__none__"}
                disabled={position.folder_id === folder.id}
                onClick={() => onMove(folder.id)}
              >
                <FolderInput className="h-3.5 w-3.5" /> {folder.label}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
      <PositionCard position={position} />
    </div>
  );
}

function FilterPill({
  label,
  count,
  active,
  onClick,
  showCount = true,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  showCount?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-[6px] border px-2.5 py-[5px] text-[12px] transition-all",
        active
          ? "border-border-strong bg-bg-active text-text"
          : "border-border bg-bg-elevated text-text-muted hover:bg-bg-hover hover:text-text",
      )}
    >
      {label}
      {showCount ? (
        <span className="ml-1.5 font-mono text-[11px] text-text-faint tabular-nums">
          · {count}
        </span>
      ) : null}
    </button>
  );
}

function SortDropdown({
  sort,
  onChange,
}: {
  sort: Sort;
  onChange: (s: Sort) => void;
}) {
  const { m } = useI18n();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handle(e: MouseEvent) {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    }
    window.addEventListener("mousedown", handle);
    return () => window.removeEventListener("mousedown", handle);
  }, [open]);

  const options: { value: Sort; label: string }[] = [
    { value: "recent", label: m.dashboard.sortRecent },
    { value: "oldest", label: m.dashboard.sortOldest },
    { value: "candidates", label: m.dashboard.sortCandidates },
    { value: "chats", label: m.dashboard.sortChats },
    { value: "title", label: m.dashboard.sortTitle },
  ];
  const current = options.find((o) => o.value === sort) ?? options[0];

  return (
    <div ref={ref} className="relative ml-auto">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-[6px] border px-2.5 py-1.5 text-[12px] font-medium transition-colors",
          open
            ? "border-border-strong bg-bg-hover text-text"
            : "border-border bg-bg-elevated text-text-muted hover:border-border-strong hover:bg-bg-hover hover:text-text",
        )}
      >
        <ListFilter className="h-3 w-3" />
        {m.dashboard.sortLabel}: {current.label}
      </button>
      {open ? (
        <div className="absolute right-0 top-full z-20 mt-1 flex w-[200px] flex-col gap-px rounded-[6px] border border-border bg-bg-elevated p-1 shadow-lg">
          {options.map((opt) => {
            const isActive = opt.value === sort;
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => {
                  onChange(opt.value);
                  setOpen(false);
                }}
                className={cn(
                  "flex items-center justify-between rounded-[4px] px-2 py-1.5 text-left text-[12px] transition-colors",
                  isActive
                    ? "bg-accent-muted text-primary"
                    : "text-text-muted hover:bg-bg-hover hover:text-text",
                )}
              >
                <span>{opt.label}</span>
                {isActive ? <Check className="h-3 w-3" /> : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
