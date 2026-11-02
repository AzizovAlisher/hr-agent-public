"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Briefcase,
  Check,
  DollarSign,
  FileText,
  Globe,
  MapPin,
  Plus,
  Upload,
  Users,
  X,
} from "lucide-react";
import { toast } from "sonner";

import {
  createPositionsFromProjectAction,
  extractProjectAction,
  type PositionDraft,
  type ProjectExtractResult,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";

const ACCEPT_FILES = ".pdf,.docx,.txt,.md";

function emptyDraft(): PositionDraft {
  return {
    title: null,
    description: null,
    requirements: null,
    seniority: null,
    work_format: null,
    location: null,
    employment_type: null,
    salary_range: null,
    team_size: null,
    folder: null,
  };
}

function formatChars(n: number): string {
  if (n < 1000) return `${n} chars`;
  return `${(n / 1000).toFixed(1)}k chars`;
}

/** @ui-only Project-extraction presentation metadata. */
type ExtractedMeta = {
  filename: string | null;
  chars: number;
};

export function NewProjectFlow() {
  const { m } = useI18n();
  const router = useRouter();

  const [positions, setPositions] = useState<PositionDraft[] | null>(null);
  const [projectName, setProjectName] = useState<string>("");
  const [folder, setFolder] = useState<string>("");
  const [meta, setMeta] = useState<ExtractedMeta | null>(null);

  const [extracting, setExtracting] = useState(false);
  const [creating, setCreating] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function handleFile(file: File) {
    if (extracting || creating) return;
    setExtracting(true);
    const fd = new FormData();
    fd.append("file", file);
    const res = await extractProjectAction(fd);
    setExtracting(false);

    if (!res.ok) {
      toast.error(m.newProject.couldNotExtractToast(res.error));
      return;
    }
    applyExtract(res.data);
  }

  function applyExtract(data: ProjectExtractResult) {
    setPositions(data.positions.length > 0 ? data.positions : []);
    setProjectName(data.project_name ?? "");
    setFolder(data.folder ?? "");
    setMeta({
      filename: data.source_filename,
      chars: data.source_chars,
    });
  }

  function onFileInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) void handleFile(f);
    e.target.value = "";
  }

  function patchPosition(idx: number, patch: Partial<PositionDraft>) {
    setPositions((prev) => {
      if (!prev) return prev;
      const next = [...prev];
      next[idx] = { ...next[idx], ...patch };
      return next;
    });
  }

  function removePosition(idx: number) {
    setPositions((prev) => {
      if (!prev) return prev;
      const next = [...prev];
      next.splice(idx, 1);
      return next;
    });
  }

  function addPosition() {
    setPositions((prev) => (prev ? [...prev, emptyDraft()] : [emptyDraft()]));
  }

  function resetUpload() {
    setPositions(null);
    setProjectName("");
    setFolder("");
    setMeta(null);
  }

  async function createAll() {
    if (!positions) return;
    const valid = positions.filter((p) => (p.title ?? "").trim().length > 0);
    if (valid.length === 0) {
      toast.error(m.newProject.noPositionsTitle);
      return;
    }
    setCreating(true);
    const res = await createPositionsFromProjectAction(
      positions,
      folder.trim() || null,
    );
    setCreating(false);

    if (!res.ok) {
      toast.error(m.newProject.couldNotCreateToast(res.error));
      return;
    }
    toast.success(m.newProject.createdToast(res.data.created.length));
    const folderId = res.data.folder_id;
    router.push(
      folderId
        ? `/dashboard?folder_id=${encodeURIComponent(folderId)}`
        : "/dashboard",
    );
  }

  const hasExtraction = positions !== null;
  const validCount =
    positions?.filter((p) => (p.title ?? "").trim().length > 0).length ?? 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="border-b border-border px-7 py-5">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="mb-1 text-[22px] font-semibold tracking-[-0.025em]">
              {m.newProject.title}
            </h1>
            <p className="max-w-[640px] text-[13px] text-text-muted">
              {m.newProject.subtitle}
            </p>
          </div>
          {hasExtraction ? (
            <button
              type="button"
              onClick={createAll}
              disabled={creating || validCount === 0}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-[6px] px-3 py-1.5 text-[12px] font-medium transition-all",
                creating || validCount === 0
                  ? "bg-bg-active text-text-faint"
                  : "bg-primary text-primary-foreground hover:opacity-90",
              )}
            >
              <Check className="h-3.5 w-3.5" />
              {creating ? m.newProject.creating : m.newProject.createAll(validCount)}
            </button>
          ) : null}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-7 py-7">
        {!hasExtraction ? (
          <UploadStep
            extracting={extracting}
            dragActive={dragActive}
            onDragEnter={() => setDragActive(true)}
            onDragLeave={() => setDragActive(false)}
            onDrop={(file) => void handleFile(file)}
            onPickClick={() => fileInputRef.current?.click()}
          />
        ) : (
          <ReviewStep
            positions={positions ?? []}
            projectName={projectName}
            folder={folder}
            meta={meta}
            onProjectNameChange={setProjectName}
            onFolderChange={setFolder}
            onPatch={patchPosition}
            onRemove={removePosition}
            onAdd={addPosition}
            onReset={resetUpload}
            disabled={creating}
          />
        )}
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept={ACCEPT_FILES}
        onChange={onFileInputChange}
        className="hidden"
      />
    </div>
  );
}

// ---------- Upload step ----------

function UploadStep({
  extracting,
  dragActive,
  onDragEnter,
  onDragLeave,
  onDrop,
  onPickClick,
}: {
  extracting: boolean;
  dragActive: boolean;
  onDragEnter: () => void;
  onDragLeave: () => void;
  onDrop: (file: File) => void;
  onPickClick: () => void;
}) {
  const { m } = useI18n();

  return (
    <div className="mx-auto flex max-w-[680px] flex-col items-center">
      <div
        onDragEnter={(e) => {
          e.preventDefault();
          onDragEnter();
        }}
        onDragOver={(e) => {
          e.preventDefault();
          onDragEnter();
        }}
        onDragLeave={(e) => {
          if (e.currentTarget.contains(e.relatedTarget as Node)) return;
          onDragLeave();
        }}
        onDrop={(e) => {
          e.preventDefault();
          onDragLeave();
          const f = e.dataTransfer.files?.[0];
          if (f) onDrop(f);
        }}
        className={cn(
          "flex w-full flex-col items-center gap-4 rounded-xl border-2 border-dashed px-6 py-12 text-center transition-colors",
          dragActive
            ? "border-accent-border bg-accent-muted"
            : "border-border bg-bg-elevated/40 hover:border-border-strong hover:bg-bg-elevated",
        )}
      >
        <span className="flex h-12 w-12 items-center justify-center rounded-[10px] border border-border bg-bg text-text-muted">
          <Upload className="h-5 w-5" />
        </span>
        <div className="flex flex-col gap-1">
          <h2 className="text-[15px] font-semibold tracking-[-0.01em]">
            {m.newProject.uploadTitle}
          </h2>
          <p className="text-[12.5px] text-text-muted">
            {m.newProject.uploadSubtitle}
          </p>
          <p className="mt-1 font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
            {m.newProject.uploadAccept}
          </p>
        </div>
        <button
          type="button"
          onClick={onPickClick}
          disabled={extracting}
          className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          <Upload className="h-3.5 w-3.5" />
          {m.newProject.uploadCta}
        </button>
        {extracting ? (
          <div className="mt-2 flex items-center gap-2 font-mono text-[11px] text-text-muted">
            <span
              className="h-1.5 w-1.5 rounded-full bg-text-muted"
              style={{ animation: "typing 1.4s ease-in-out infinite" }}
            />
            <span
              className="h-1.5 w-1.5 rounded-full bg-text-muted"
              style={{ animation: "typing 1.4s ease-in-out 0.2s infinite" }}
            />
            <span
              className="h-1.5 w-1.5 rounded-full bg-text-muted"
              style={{ animation: "typing 1.4s ease-in-out 0.4s infinite" }}
            />
            <span>{m.newProject.extracting}</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ---------- Review step ----------

function ReviewStep({
  positions,
  projectName,
  folder,
  meta,
  onProjectNameChange,
  onFolderChange,
  onPatch,
  onRemove,
  onAdd,
  onReset,
  disabled,
}: {
  positions: PositionDraft[];
  projectName: string;
  folder: string;
  meta: ExtractedMeta | null;
  onProjectNameChange: (v: string) => void;
  onFolderChange: (v: string) => void;
  onPatch: (idx: number, patch: Partial<PositionDraft>) => void;
  onRemove: (idx: number) => void;
  onAdd: () => void;
  onReset: () => void;
  disabled: boolean;
}) {
  const { m } = useI18n();

  return (
    <div className="mx-auto flex max-w-[920px] flex-col gap-5">
      {/* Project metadata strip */}
      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.newProject.projectNameLabel}
            </span>
            <input
              type="text"
              value={projectName}
              onChange={(e) => onProjectNameChange(e.target.value)}
              placeholder={m.newProject.projectNamePlaceholder}
              disabled={disabled}
              className="rounded-[6px] border border-border bg-bg px-2.5 py-1.5 text-[13px] outline-none placeholder:text-text-faint focus:border-border-strong"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.newProject.folderLabel}
            </span>
            <input
              type="text"
              value={folder}
              onChange={(e) => onFolderChange(e.target.value)}
              placeholder={m.newProject.folderPlaceholder}
              disabled={disabled}
              className="rounded-[6px] border border-border bg-bg px-2.5 py-1.5 text-[13px] outline-none placeholder:text-text-faint focus:border-border-strong"
            />
            <span className="text-[11px] text-text-faint">
              {m.newProject.folderHint}
            </span>
          </label>
        </div>
        {meta?.filename ? (
          <div className="mt-4 flex items-center justify-between gap-3 border-t border-border pt-3">
            <div className="inline-flex min-w-0 items-center gap-2 font-mono text-[11px] text-text-faint">
              <FileText className="h-3 w-3 shrink-0" />
              <span className="truncate">
                {m.newProject.sourceMeta(
                  meta.filename,
                  formatChars(meta.chars),
                )}
              </span>
            </div>
            <button
              type="button"
              onClick={onReset}
              disabled={disabled}
              className="inline-flex items-center gap-1 rounded-[6px] border border-border px-2 py-1 text-[11px] text-text-muted transition-colors hover:bg-bg-hover hover:text-text disabled:opacity-40"
            >
              {m.newProject.resetUpload}
            </button>
          </div>
        ) : null}
      </section>

      {/* Empty state */}
      {positions.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-bg-elevated/40 p-8 text-center">
          <h3 className="mb-1 text-[14px] font-semibold">
            {m.newProject.noPositionsTitle}
          </h3>
          <p className="mx-auto max-w-[520px] text-[12.5px] text-text-muted">
            {m.newProject.noPositionsDesc}
          </p>
          <div className="mt-4 flex justify-center gap-2">
            <button
              type="button"
              onClick={onAdd}
              disabled={disabled}
              className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-3 py-1.5 text-[12px] font-medium text-text transition-colors hover:bg-bg-hover disabled:opacity-40"
            >
              <Plus className="h-3 w-3" />
              {m.newProject.addPosition}
            </button>
          </div>
        </div>
      ) : (
        <>
          <div>
            <div className="text-[13px] font-medium">
              {m.newProject.extractedHeading(positions.length)}
            </div>
            <p className="mt-0.5 text-[12px] text-text-muted">
              {m.newProject.extractedHint}
            </p>
          </div>

          <ul className="flex flex-col gap-3">
            {positions.map((p, i) => (
              <PositionRow
                key={i}
                index={i}
                draft={p}
                onPatch={(patch) => onPatch(i, patch)}
                onRemove={() => onRemove(i)}
                disabled={disabled}
              />
            ))}
          </ul>

          <div>
            <button
              type="button"
              onClick={onAdd}
              disabled={disabled}
              className="inline-flex items-center gap-1.5 rounded-[6px] border border-dashed border-border px-3 py-2 text-[12px] font-medium text-text-muted transition-colors hover:border-border-strong hover:text-text disabled:opacity-40"
            >
              <Plus className="h-3 w-3" />
              {m.newProject.addPosition}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// ---------- Single position row ----------

function PositionRow({
  index,
  draft,
  onPatch,
  onRemove,
  disabled,
}: {
  index: number;
  draft: PositionDraft;
  onPatch: (patch: Partial<PositionDraft>) => void;
  onRemove: () => void;
  disabled: boolean;
}) {
  const { m } = useI18n();

  return (
    <li className="rounded-xl border border-border bg-bg-elevated p-5">
      <div className="flex items-start gap-3">
        <span className="mt-1 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-[6px] bg-bg font-mono text-[10px] text-text-muted">
          {index + 1}
        </span>

        <div className="min-w-0 flex-1">
          <input
            type="text"
            value={draft.title ?? ""}
            onChange={(e) => onPatch({ title: e.target.value || null })}
            placeholder={m.newProject.titlePlaceholder}
            disabled={disabled}
            className={cn(
              "block w-full rounded-[6px] border border-transparent bg-transparent px-2 py-1 text-[16px] font-semibold tracking-[-0.015em] outline-none placeholder:text-text-faint hover:border-border focus:border-border-strong",
              !(draft.title ?? "").trim() && "italic text-text-faint",
            )}
          />

          <textarea
            value={draft.description ?? ""}
            onChange={(e) => onPatch({ description: e.target.value || null })}
            placeholder={m.newProject.descriptionPlaceholder}
            disabled={disabled}
            rows={2}
            className="mt-2 block w-full resize-y rounded-[6px] border border-transparent bg-transparent px-2 py-1 text-[13px] leading-[1.5] text-text-muted outline-none placeholder:text-text-faint hover:border-border focus:border-border-strong"
          />

          <div className="mt-3">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.newProject.requirementsLabel}
            </div>
            <RequirementsTags
              value={draft.requirements ?? null}
              onChange={(v) => onPatch({ requirements: v })}
              disabled={disabled}
            />
          </div>

          <div className="mt-4 grid grid-cols-1 gap-1.5 border-t border-border pt-3 sm:grid-cols-2 lg:grid-cols-3">
            <InlineField
              icon={<Briefcase className="h-3 w-3" />}
              label={m.newPosition.fieldSeniority}
              value={draft.seniority}
              onChange={(v) => onPatch({ seniority: v })}
              disabled={disabled}
              placeholder={m.newPosition.placeholderSeniority}
            />
            <InlineField
              icon={<Globe className="h-3 w-3" />}
              label={m.newPosition.fieldWorkFormat}
              value={draft.work_format}
              onChange={(v) => onPatch({ work_format: v })}
              disabled={disabled}
              placeholder={m.newPosition.placeholderWorkFormat}
            />
            <InlineField
              icon={<MapPin className="h-3 w-3" />}
              label={m.newPosition.fieldLocation}
              value={draft.location}
              onChange={(v) => onPatch({ location: v })}
              disabled={disabled}
              placeholder={m.newPosition.placeholderLocation}
            />
            <InlineField
              icon={<Briefcase className="h-3 w-3" />}
              label={m.newPosition.fieldEmployment}
              value={draft.employment_type}
              onChange={(v) => onPatch({ employment_type: v })}
              disabled={disabled}
              placeholder={m.newPosition.placeholderEmployment}
            />
            <InlineField
              icon={<DollarSign className="h-3 w-3" />}
              label={m.newPosition.fieldSalary}
              value={draft.salary_range}
              onChange={(v) => onPatch({ salary_range: v })}
              disabled={disabled}
              placeholder={m.newPosition.placeholderSalary}
            />
            <InlineField
              icon={<Users className="h-3 w-3" />}
              label={m.newPosition.fieldTeamSize}
              value={draft.team_size}
              onChange={(v) => onPatch({ team_size: v })}
              disabled={disabled}
              placeholder={m.newPosition.placeholderTeamSize}
            />
          </div>
        </div>

        <button
          type="button"
          onClick={onRemove}
          disabled={disabled}
          title={m.newProject.removePosition}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[6px] text-text-faint transition-colors hover:bg-bg-hover hover:text-score-low disabled:opacity-40"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

    </li>
  );
}

function InlineField({
  icon,
  label,
  value,
  placeholder,
  onChange,
  disabled,
}: {
  icon: React.ReactNode;
  label: string;
  value: string | null;
  placeholder: string;
  onChange: (v: string | null) => void;
  disabled: boolean;
}) {
  return (
    <div className="flex items-center gap-2 rounded-[6px] px-2 py-1 transition-colors hover:bg-bg-hover">
      <span className="flex h-3 w-3 items-center justify-center text-text-faint">
        {icon}
      </span>
      <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
        {label}
      </span>
      <input
        type="text"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value || null)}
        placeholder={placeholder}
        disabled={disabled}
        className="ml-auto w-0 min-w-0 flex-1 truncate border-none bg-transparent text-right text-[12px] text-text outline-none placeholder:text-text-faint"
      />
    </div>
  );
}

function RequirementsTags({
  value,
  onChange,
  disabled,
}: {
  value: Record<string, unknown> | null;
  onChange: (v: Record<string, unknown> | null) => void;
  disabled: boolean;
}) {
  const { m } = useI18n();
  const entries = Object.entries(value ?? {});
  const [adding, setAdding] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");

  function remove(key: string) {
    const next = { ...(value ?? {}) };
    delete next[key];
    onChange(Object.keys(next).length ? next : null);
  }

  function addNew() {
    const k = newKey.trim();
    if (!k) {
      setAdding(false);
      setNewKey("");
      setNewValue("");
      return;
    }
    onChange({ ...(value ?? {}), [k]: newValue.trim() || "required" });
    setNewKey("");
    setNewValue("");
    setAdding(false);
  }

  return (
    <ul className="flex flex-wrap items-center gap-1.5">
      {entries.map(([k, v]) => (
        <li
          key={k}
          className="group flex items-center gap-1 rounded-[4px] border border-border bg-bg px-2 py-1 font-mono text-[10.5px] tabular-nums text-text-muted"
        >
          <span className="text-text">{k}</span>
          <span className="text-text-faint">·</span>
          <span>{String(v)}</span>
          <button
            type="button"
            onClick={() => remove(k)}
            disabled={disabled}
            className="ml-0.5 -mr-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-[3px] text-text-faint opacity-0 transition-all hover:bg-bg-active hover:text-text group-hover:opacity-100 disabled:opacity-0"
          >
            <X className="h-2.5 w-2.5" />
          </button>
        </li>
      ))}
      {adding ? (
        <li className="flex items-center gap-1 rounded-[4px] border border-border-strong bg-bg px-1.5 py-[3px]">
          <input
            autoFocus
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setAdding(false);
                setNewKey("");
                setNewValue("");
              }
            }}
            placeholder={m.newPosition.keyPlaceholder}
            disabled={disabled}
            className="w-[80px] border-none bg-transparent font-mono text-[10.5px] text-text outline-none placeholder:text-text-faint"
          />
          <span className="font-mono text-[10.5px] text-text-faint">·</span>
          <input
            value={newValue}
            onChange={(e) => setNewValue(e.target.value)}
            onBlur={addNew}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addNew();
              } else if (e.key === "Escape") {
                setAdding(false);
                setNewKey("");
                setNewValue("");
              }
            }}
            placeholder={m.newPosition.valuePlaceholder}
            disabled={disabled}
            className="w-[100px] border-none bg-transparent font-mono text-[10.5px] tabular-nums text-text outline-none placeholder:text-text-faint"
          />
        </li>
      ) : (
        <li>
          <button
            type="button"
            onClick={() => setAdding(true)}
            disabled={disabled}
            className="flex items-center gap-1 rounded-[4px] border border-dashed border-border px-2 py-1 font-mono text-[10.5px] text-text-faint transition-colors hover:border-border-strong hover:text-text-muted disabled:opacity-40"
          >
            <Plus className="h-2.5 w-2.5" />
            {m.newPosition.addReq}
          </button>
        </li>
      )}
    </ul>
  );
}
