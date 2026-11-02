"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Check,
  FileText,
  Folder,
  FolderUp,
  Globe,
  Loader2,
  MinusCircle,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  scoreAllOpenAction,
  uploadAndScoreAction,
  uploadCandidateFileAction,
} from "@/lib/actions";
import { cn } from "@/lib/utils";
import type { Folder as FolderNode } from "@/lib/types";
import { FolderScopePicker } from "@/components/candidates/folder-scope-picker";

/** @ui-only Bulk-import scoring selection. */
type ScoreScope = "none" | "all" | "folder";

const MAX_BYTES = 15 * 1024 * 1024;
const ACCEPT_EXT = [".pdf", ".docx"];
const PARALLEL = 3;

/** @ui-only Client upload-row lifecycle. */
type Status = "queued" | "uploading" | "scoring_queued" | "done" | "failed";

/** @ui-only Client upload queue item. */
type Item = {
  id: string;
  file: File;
  status: Status;
  error: string | null;
};

let _localId = 0;
const nextId = () => `f${++_localId}`;

function isAccepted(f: File): boolean {
  const name = f.name.toLowerCase();
  return ACCEPT_EXT.some((ext) => name.endsWith(ext));
}

/** @ui-only Bulk-upload dialog composition properties. */
type Props = {
  /** Position-scoped mode: every uploaded CV is scored against this one position. */
  positionId?: string;
  /** Org-wide mode (candidates page): scope picker controls scoring behavior. */
  folders?: string[];
  folderTree?: FolderNode[];
  openPositionCount?: number;
};

/**
 * Bulk-upload N CV files. Two modes:
 *  1. positionId set → each file is uploaded + scored against that position
 *     (original behavior, used on the position detail page).
 *  2. positionId omitted → the recruiter picks a scoring scope (none / all
 *     open / a single folder) and each file goes through the matching path.
 *
 * Concurrency cap (`PARALLEL`) is kept conservative because each upload now
 * also fans out N concurrent scoring calls server-side; we don't want to
 * stack too many in-flight LLM batches at once.
 */
export function BulkUploadDialog({
  positionId,
  folders = [],
  folderTree = [],
  openPositionCount = 0,
}: Props) {
  const router = useRouter();
  const { m } = useI18n();
  const t = m.bulkUpload;
  const positionMode = !!positionId;

  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Item[]>([]);
  const [running, setRunning] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [scope, setScope] = useState<ScoreScope>("none");
  const [folderId, setFolderId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const total = items.length;
  const done = items.filter((i) => i.status === "done").length;
  const scoringQueued = items.filter((i) => i.status === "scoring_queued").length;
  const failed = items.filter((i) => i.status === "failed").length;
  const allFinal =
    total > 0 &&
    items.every(
      (i) => i.status === "done" || i.status === "scoring_queued" || i.status === "failed",
    );

  function addFiles(files: FileList | File[]) {
    const list = Array.from(files);
    const additions: Item[] = [];
    for (const f of list) {
      if (!isAccepted(f)) {
        toast.error(`${f.name}: ${t.unsupportedType}`);
        continue;
      }
      if (f.size > MAX_BYTES) {
        toast.error(`${f.name}: ${t.skippedTooBig((f.size / 1024).toFixed(0))}`);
        continue;
      }
      additions.push({ id: nextId(), file: f, status: "queued", error: null });
    }
    if (additions.length > 0) {
      setItems((prev) => [...prev, ...additions]);
    }
  }

  function onDrop(e: React.DragEvent<HTMLLabelElement>) {
    e.preventDefault();
    setDragActive(false);
    if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files);
  }

  function clearAll() {
    if (running) return;
    setItems([]);
  }

  function removeOne(id: string) {
    if (running) return;
    setItems((prev) => prev.filter((i) => i.id !== id));
  }

  async function processOne(item: Item): Promise<void> {
    setItems((prev) =>
      prev.map((p) => (p.id === item.id ? { ...p, status: "uploading" } : p)),
    );

    let ok = false;
    let queuedForScoring = false;
    let errMsg: string | null = null;

    if (positionMode && positionId) {
      const fd = new FormData();
      fd.set("kind", "file");
      fd.set("file", item.file);
      const res = await uploadAndScoreAction(fd, positionId);
      ok = res.ok;
      queuedForScoring = res.ok && res.data.status !== "succeeded";
      errMsg = res.ok ? null : res.error;
    } else {
      // Org-wide mode: upload, then route to the scoring action chosen in the
      // scope picker. "none" finishes after the upload succeeds.
      const fd = new FormData();
      fd.set("file", item.file);
      const up = await uploadCandidateFileAction(fd);
      if (!up.ok) {
        ok = false;
        errMsg = up.error;
      } else if (scope === "none") {
        ok = true;
      } else {
        // scope "all" → folderId null (every open position); scope "folder" →
        // the chosen folder_id (parent = whole subtree).
        const fid = scope === "folder" ? folderId : null;
        const sc = await scoreAllOpenAction(up.data.id, null, fid);
        ok = sc.ok;
        queuedForScoring = sc.ok && sc.data.queued > 0;
        errMsg = sc.ok ? null : sc.error;
      }
    }

    setItems((prev) =>
      prev.map((p) =>
        p.id === item.id
          ? {
              ...p,
              status: ok ? (queuedForScoring ? "scoring_queued" : "done") : "failed",
              error: errMsg,
            }
          : p,
      ),
    );
  }

  async function start() {
    if (running) return;
    const queue = items.filter((i) => i.status === "queued");
    if (queue.length === 0) return;
    setRunning(true);

    // Bounded concurrency: run up to PARALLEL processOne calls in parallel.
    let cursor = 0;
    async function worker() {
      while (cursor < queue.length) {
        const idx = cursor++;
        const item = queue[idx];
        if (item) await processOne(item);
      }
    }
    await Promise.all(
      Array.from({ length: Math.min(PARALLEL, queue.length) }, () => worker()),
    );

    setRunning(false);
    // Pull fresh state to count actual outcomes. Toast on either successes
    // (positive feedback) OR all-failures (so the recruiter doesn't think
    // the operation hung silently). Per-item error rows stay visible too.
    setItems((curr) => {
      const successes = curr.filter(
        (i) => i.status === "done" || i.status === "scoring_queued",
      ).length;
      const failures = curr.filter((i) => i.status === "failed").length;
      if (successes > 0) {
        toast.success(t.completeToast(successes));
      } else if (failures > 0) {
        toast.error(t.allFailedToast(failures));
      }
      return curr;
    });
    router.refresh();
  }

  function close() {
    if (running) return;
    setOpen(false);
    // Preserve list across reopens within the same session — recruiter
    // might want to add more without losing context.
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v && running) return;
        setOpen(v);
      }}
    >
      <DialogTrigger
        className={cn(
          "inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-2.5 py-1.5 text-[12px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        )}
      >
        <FolderUp className="h-3 w-3" />
        {t.button}
      </DialogTrigger>
      <DialogContent
        className="gap-0 p-0 sm:max-w-[600px]"
        showCloseButton={false}
      >
        <div className="flex flex-col gap-1 border-b border-border px-5 py-4">
          <DialogTitle className="text-[14px] tracking-tight">
            {t.dialogTitle}
          </DialogTitle>
          <DialogDescription className="text-[12px] text-text-muted">
            {positionMode ? t.dialogDesc : t.dialogDescOrgWide}
          </DialogDescription>
        </div>

        <div className="flex flex-col gap-3 px-5 py-4">
          {!positionMode ? (
            <BulkScopePicker
              scope={scope}
              onChange={setScope}
              folderTree={folderTree}
              folderId={folderId}
              onFolderChange={setFolderId}
              openPositionCount={openPositionCount}
              disabled={running}
            />
          ) : null}
          <label
            onDragEnter={(e) => {
              e.preventDefault();
              if (!running) setDragActive(true);
            }}
            onDragOver={(e) => {
              e.preventDefault();
              if (!running) setDragActive(true);
            }}
            onDragLeave={() => setDragActive(false)}
            onDrop={onDrop}
            className={cn(
              "relative flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border-strong bg-bg px-6 py-6 text-center transition-all",
              dragActive && "border-accent-border bg-accent-muted",
              running && "cursor-not-allowed opacity-60",
            )}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              multiple
              disabled={running}
              onChange={(e) => {
                if (e.target.files) addFiles(e.target.files);
                if (fileInputRef.current) fileInputRef.current.value = "";
              }}
              className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
            />
            <div className="flex h-9 w-9 items-center justify-center rounded-[8px] border border-border bg-bg-elevated text-text-muted">
              <FolderUp className="h-[18px] w-[18px]" />
            </div>
            <div className="text-[13px] font-medium">{t.dropTitle}</div>
            <div className="font-mono text-[11px] tracking-[0.02em] text-text-faint">
              {t.dropSub}
            </div>
          </label>

          {items.length > 0 ? (
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between">
                <span className="font-mono text-[10.5px] text-text-faint">
                  {t.summary(total, done, scoringQueued, failed)}
                </span>
                <button
                  type="button"
                  onClick={clearAll}
                  disabled={running}
                  className="text-[11px] text-text-muted transition-colors hover:text-text disabled:opacity-40"
                >
                  {t.clearAll}
                </button>
              </div>
              <ul className="max-h-[240px] overflow-y-auto rounded-md border border-border bg-bg">
                {items.map((it) => (
                  <li
                    key={it.id}
                    className="flex items-center gap-2.5 border-b border-border px-3 py-2 last:border-b-0"
                  >
                    <FileText className="h-3.5 w-3.5 shrink-0 text-text-faint" />
                    <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-text">
                      {it.file.name}
                    </span>
                    <StatusPill status={it.status} t={t} />
                    {it.status === "queued" ? (
                      <button
                        type="button"
                        onClick={() => removeOne(it.id)}
                        className="text-text-faint transition-colors hover:text-text"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={close}
              disabled={running}
              className="rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-[12px] text-text-muted transition-colors hover:border-border-strong hover:text-text disabled:opacity-40"
            >
              {allFinal ? t.close : t.cancel}
            </button>
            <button
              type="button"
              onClick={start}
              disabled={
                running ||
                items.filter((i) => i.status === "queued").length === 0
              }
              className="inline-flex items-center gap-1.5 rounded-md bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {running ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  {t.processing}
                </>
              ) : (
                t.start
              )}
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function BulkScopePicker({
  scope,
  onChange,
  folderTree,
  folderId,
  onFolderChange,
  openPositionCount,
  disabled,
}: {
  scope: ScoreScope;
  onChange: (s: ScoreScope) => void;
  folderTree: FolderNode[];
  folderId: string | null;
  onFolderChange: (id: string) => void;
  openPositionCount: number;
  disabled: boolean;
}) {
  const { m } = useI18n();
  const folderAvailable = folderTree.length > 0;

  type Opt = { id: ScoreScope; label: string; icon: React.ReactNode };
  const options: Opt[] = [
    {
      id: "none",
      label: m.candidates.add.scopeNoneShort,
      icon: <MinusCircle className="h-3 w-3" />,
    },
    {
      id: "all",
      label: m.candidates.add.scopeAllShort,
      icon: <Globe className="h-3 w-3" />,
    },
    ...(folderAvailable
      ? [
          {
            id: "folder" as ScoreScope,
            label: m.candidates.add.scopeFolderShort,
            icon: <Folder className="h-3 w-3" />,
          },
        ]
      : []),
  ];

  // One-liner hint that swaps based on the chosen scope. Keeps the picker
  // visually compact while still telling the recruiter what'll happen.
  const hint =
    scope === "none"
      ? m.candidates.add.scopeNoneHint
      : scope === "all"
        ? openPositionCount > 0
          ? m.candidates.add.scopeAllHint(openPositionCount)
          : m.candidates.add.scopeAllEmpty
        : m.candidates.add.scopeFolderHint;

  return (
    <div className="flex w-full flex-col gap-2">
      <div className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
        {m.candidates.add.scopeLabel}
      </div>
      <div className="flex w-full items-center gap-1 rounded-[6px] border border-border bg-bg p-0.5">
        {options.map((opt) => {
          const active = scope === opt.id;
          const isAllDisabled = opt.id === "all" && openPositionCount === 0;
          return (
            <button
              key={opt.id}
              type="button"
              onClick={() => onChange(opt.id)}
              disabled={disabled || isAllDisabled}
              className={cn(
                "flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-[5px] px-2 py-1.5 text-[11.5px] font-medium transition-colors",
                active
                  ? "bg-bg-elevated text-text shadow-sm"
                  : "text-text-muted hover:text-text",
                (disabled || isAllDisabled) && "opacity-40",
              )}
            >
              <span className="flex h-3 w-3 shrink-0 items-center justify-center text-text-muted">
                {opt.icon}
              </span>
              <span className="truncate">{opt.label}</span>
            </button>
          );
        })}
      </div>
      <div className="flex flex-col gap-2">
        <span className="text-[11px] leading-snug text-text-faint">{hint}</span>
        {scope === "folder" && folderAvailable ? (
          <FolderScopePicker
            tree={folderTree}
            value={folderId}
            onChange={onFolderChange}
            disabled={disabled}
          />
        ) : null}
      </div>
    </div>
  );
}

function StatusPill({
  status,
  t,
}: {
  status: Status;
  t: {
    rowQueued: string;
    rowUploading: string;
    rowScoringQueued: string;
    rowDone: string;
    rowFailed: string;
  };
}) {
  if (status === "queued") {
    return (
      <span className="font-mono text-[10px] text-text-faint">
        {t.rowQueued}
      </span>
    );
  }
  if (status === "uploading") {
    return (
      <span className="inline-flex items-center gap-1 font-mono text-[10px] text-text-muted">
        <Loader2 className="h-2.5 w-2.5 animate-spin" />
        {t.rowUploading}
      </span>
    );
  }
  if (status === "scoring_queued") {
    return (
      <span className="inline-flex items-center gap-1 font-mono text-[10px] text-accent">
        <Check className="h-3 w-3" />
        {t.rowScoringQueued}
      </span>
    );
  }
  if (status === "done") {
    return (
      <span className="inline-flex items-center gap-1 font-mono text-[10px] text-primary">
        <Check className="h-3 w-3" />
        {t.rowDone}
      </span>
    );
  }
  return (
    <span className="font-mono text-[10px] text-score-low">{t.rowFailed}</span>
  );
}
