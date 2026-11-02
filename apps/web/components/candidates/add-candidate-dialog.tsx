"use client";

import { useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  ClipboardPaste,
  Code2,
  FileText,
  Folder,
  Globe,
  MinusCircle,
  Plus,
  Upload,
} from "lucide-react";
import { toast } from "sonner";

import {
  scoreAllOpenAction,
  uploadCandidateFileAction,
  uploadCandidateHHJsonAction,
  uploadCandidateTextAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";
import type { Folder as FolderNode } from "@/lib/types";
import { FolderScopePicker } from "@/components/candidates/folder-scope-picker";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/** @ui-only Candidate-import input mode. */
type Tab = "file" | "text" | "hh";
/** @ui-only Candidate-import scoring selection. */
type ScoreScope = "none" | "all" | "folder";

/** @ui-only Candidate-dialog composition properties. */
type Props = {
  /** Distinct folder names that contain at least one OPEN position (legacy). */
  folders?: string[];
  /** Two-level folder tree for the drill-down scope picker. */
  folderTree?: FolderNode[];
  /** Total count of open positions across the org (for the "All" label). */
  openPositionCount?: number;
};

/**
 * Add a candidate to the org, optionally scoring them against open positions
 * right away. Three scope choices: skip scoring (manual later from the
 * candidate page), score against every open position, or score against just
 * the open positions in a chosen folder.
 */
export function AddCandidateDialog({
  folders = [],
  folderTree = [],
  openPositionCount = 0,
}: Props) {
  const { m } = useI18n();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("file");
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [hhJson, setHhJson] = useState("");
  // Default to "none" so we never silently fire scoring on a recruiter who
  // didn't ask for it — the picker makes the intent explicit.
  const [scope, setScope] = useState<ScoreScope>("none");
  const [folderId, setFolderId] = useState<string | null>(null);
  const [scoringStage, setScoringStage] = useState<
    "idle" | "uploading" | "scoring"
  >("idle");

  function reset() {
    setFile(null);
    setText("");
    setHhJson("");
    setError(null);
    setScope("none");
    setFolderId(null);
    setScoringStage("idle");
  }

  function close() {
    setOpen(false);
    reset();
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (pending) return;
    setError(null);

    start(async () => {
      setScoringStage("uploading");
      let res: { ok: true; data: { id: string } } | { ok: false; error: string };
      if (tab === "file") {
        if (!file) {
          setError(m.candidates.add.errors.noFile);
          setScoringStage("idle");
          return;
        }
        const MAX_BYTES = 15 * 1024 * 1024;
        if (file.size > MAX_BYTES) {
          setError(
            m.candidates.add.errors.fileTooLarge(
              `${(file.size / (1024 * 1024)).toFixed(1)} ${m.common.unitMb}`,
              `15 ${m.common.unitMb}`,
            ),
          );
          setScoringStage("idle");
          return;
        }
        const fd = new FormData();
        fd.set("file", file);
        res = await uploadCandidateFileAction(fd);
      } else if (tab === "text") {
        const t = text.trim();
        if (t.length < 50) {
          setError(m.candidates.add.errors.textTooShort);
          setScoringStage("idle");
          return;
        }
        res = await uploadCandidateTextAction(t);
      } else {
        const t = hhJson.trim();
        if (!t) {
          setError(m.candidates.add.errors.noHh);
          setScoringStage("idle");
          return;
        }
        res = await uploadCandidateHHJsonAction(t);
      }

      if (!res.ok) {
        setError(res.error);
        setScoringStage("idle");
        return;
      }
      toast.success(m.candidates.add.createdToast);
      const newId = res.data.id;

      // Optional follow-up scoring is durable. We wait only for the queue
      // receipt, then navigate; results appear as workers complete the jobs.
      if (scope === "all") {
        setScoringStage("scoring");
        const r = await scoreAllOpenAction(newId, null);
        if (!r.ok) {
          toast.error(m.candidates.add.scoreFailedToast(r.error));
        } else {
          toast.success(m.candidates.add.scoredToast(r.data.queued));
        }
      } else if (scope === "folder" && folderId) {
        setScoringStage("scoring");
        const r = await scoreAllOpenAction(newId, null, folderId);
        if (!r.ok) {
          toast.error(m.candidates.add.scoreFailedToast(r.error));
        } else {
          toast.success(m.candidates.add.scoredToast(r.data.queued));
        }
      }

      close();
      router.push(`/candidates/${newId}`);
      router.refresh();
    });
  }

  const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
    {
      id: "file",
      label: m.position.upload.tabFile,
      icon: <FileText className="h-3 w-3" />,
    },
    {
      id: "text",
      label: m.position.upload.tabText,
      icon: <ClipboardPaste className="h-3 w-3" />,
    },
    {
      id: "hh",
      label: m.position.upload.tabHh,
      icon: <Code2 className="h-3 w-3" />,
    },
  ];

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90"
      >
        <Plus className="h-3.5 w-3.5" />
        {m.candidates.add.button}
      </button>

      <Dialog open={open} onOpenChange={(o) => (o ? setOpen(true) : close())}>
        <DialogContent className="sm:max-w-[560px]">
          <DialogHeader>
            <DialogTitle className="text-[15px]">
              {m.candidates.add.title}
            </DialogTitle>
            <DialogDescription className="text-[12px] text-text-muted">
              {m.candidates.add.description}
            </DialogDescription>
          </DialogHeader>

          <form onSubmit={submit} className="flex flex-col gap-4">
            {/* Tabs */}
            <div className="flex items-center gap-1 rounded-[6px] border border-border bg-bg p-0.5">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => {
                    setTab(t.id);
                    setError(null);
                  }}
                  className={cn(
                    "flex flex-1 items-center justify-center gap-1.5 rounded-[5px] px-2.5 py-1.5 text-[12px] font-medium transition-colors",
                    tab === t.id
                      ? "bg-bg-elevated text-text"
                      : "text-text-muted hover:text-text",
                  )}
                >
                  {t.icon}
                  {t.label}
                </button>
              ))}
            </div>

            {/* File tab */}
            {tab === "file" ? (
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                className="group flex items-center gap-3 rounded-lg border border-dashed border-border bg-bg/40 px-4 py-4 text-left transition-colors hover:border-accent-border hover:bg-accent-muted"
              >
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[8px] border border-border bg-bg-elevated text-text-muted group-hover:border-accent-border group-hover:text-primary">
                  {file ? (
                    <FileText className="h-4 w-4" />
                  ) : (
                    <Upload className="h-4 w-4" />
                  )}
                </span>
                <span className="flex min-w-0 flex-1 flex-col">
                  {file ? (
                    <>
                      <span className="truncate font-mono text-[12px] text-text">
                        {file.name}
                      </span>
                      <span className="font-mono text-[10px] text-text-faint">
                        {(file.size / 1024).toFixed(0)} {m.common.unitKb} ·{" "}
                        {m.candidates.add.replaceFile}
                      </span>
                    </>
                  ) : (
                    <>
                      <span className="text-[13px] font-medium text-text">
                        {m.candidates.add.pickFile}
                      </span>
                      <span className="text-[11px] text-text-muted">
                        PDF · DOCX
                      </span>
                    </>
                  )}
                </span>
                <input
                  ref={fileRef}
                  type="file"
                  accept=".pdf,.docx"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) setFile(f);
                  }}
                  className="hidden"
                />
              </button>
            ) : null}

            {/* Text tab */}
            {tab === "text" ? (
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={10}
                placeholder={m.position.upload.pasteTextPlaceholder}
                className="block w-full resize-y rounded-[6px] border border-border bg-bg px-3 py-2 text-[12.5px] leading-[1.55] outline-none placeholder:text-text-faint focus:border-border-strong"
              />
            ) : null}

            {/* HH JSON tab */}
            {tab === "hh" ? (
              <textarea
                value={hhJson}
                onChange={(e) => setHhJson(e.target.value)}
                rows={10}
                placeholder={m.position.upload.pasteHhPlaceholder}
                className="block w-full resize-y rounded-[6px] border border-border bg-bg px-3 py-2 font-mono text-[11px] leading-[1.5] outline-none placeholder:text-text-faint focus:border-border-strong"
              />
            ) : null}

            {/* Scoring scope — runs right after the candidate is created. */}
            <ScopePicker
              scope={scope}
              onChange={(s) => setScope(s)}
              folderTree={folderTree}
              folderId={folderId}
              onFolderChange={setFolderId}
              openPositionCount={openPositionCount}
              disabled={pending}
            />

            {error ? (
              <div className="rounded-[6px] border border-score-low/30 bg-score-low/10 px-3 py-2 text-[12px] text-score-low">
                {error}
              </div>
            ) : null}

            <div className="flex items-center justify-between gap-3 pt-1">
              <span className="text-[10.5px] text-text-faint">
                {scope === "none"
                  ? m.candidates.add.scoreLater
                  : m.candidates.add.scoreNowHint}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={close}
                  className="rounded-[6px] px-3 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
                >
                  {m.common.cancel}
                </button>
                <button
                  type="submit"
                  disabled={pending}
                  className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
                >
                  {pending
                    ? scoringStage === "scoring"
                      ? m.candidates.add.scoring
                      : m.candidates.add.adding
                    : m.candidates.add.add}
                </button>
              </div>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

function ScopePicker({
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
  // Hide the "Folder" option entirely when there are no folders — the picker
  // would dead-end otherwise.
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
        <span className="text-[11px] leading-snug text-text-faint">
          {hint}
        </span>
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
