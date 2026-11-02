"use client";

import { useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  ClipboardPaste,
  Code2,
  FileText,
  Upload,
} from "lucide-react";

import {
  getCandidateProcessingJobAction,
  uploadAndScoreAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

/** @ui-only Candidate-upload input mode. */
type Tab = "file" | "text" | "hh";

export function UploadForm({ positionId }: { positionId: string }) {
  const { m } = useI18n();
  const router = useRouter();
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>("file");
  const formRef = useRef<HTMLFormElement>(null);

  const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
    {
      id: "file",
      label: m.position.upload.tabFile,
      icon: <FileText className="h-3.5 w-3.5" />,
    },
    {
      id: "text",
      label: m.position.upload.tabText,
      icon: <ClipboardPaste className="h-3.5 w-3.5" />,
    },
    {
      id: "hh",
      label: m.position.upload.tabHh,
      icon: <Code2 className="h-3.5 w-3.5" />,
    },
  ];

  // File picker UX state
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);

  function submit(e?: React.FormEvent) {
    e?.preventDefault();
    if (!formRef.current) return;
    const fd = new FormData(formRef.current);
    fd.set("kind", activeTab);
    setError(null);
    start(async () => {
      const res = await uploadAndScoreAction(fd, positionId);
      if (res.ok) {
        toast.success(m.position.upload.candidateQueuedToast);
        formRef.current?.reset();
        setFileName(null);
        void pollScoringJob(res.data.candidate_id, res.data.id);
      } else {
        setError(res.error);
      }
    });
  }

  async function pollScoringJob(candidateId: string, jobId: string) {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 2_000));
      const job = await getCandidateProcessingJobAction(candidateId, jobId);
      if (!job.ok) continue;
      if (job.data.status === "succeeded") {
        toast.success(m.position.upload.candidateScoredToast);
        router.refresh();
        return;
      }
      if (job.data.status === "dead_letter" || job.data.status === "cancelled") {
        toast.error(
          m.position.upload.candidateScoreFailedToast(
            job.data.failure_detail || job.data.failure_code || job.data.status,
          ),
        );
        return;
      }
    }
    toast.info(m.position.upload.candidateStillScoringToast);
  }

  function onFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    setFileName(e.target.files?.[0]?.name ?? null);
  }

  function onDrop(e: React.DragEvent<HTMLLabelElement>) {
    e.preventDefault();
    setDragActive(false);
    const f = e.dataTransfer.files?.[0];
    if (f && fileInputRef.current) {
      const dt = new DataTransfer();
      dt.items.add(f);
      fileInputRef.current.files = dt.files;
      setFileName(f.name);
    }
  }

  return (
    <div>
      {/* Tabs */}
      <div className="flex border-b border-border px-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setActiveTab(t.id)}
            className={cn(
              "-mb-px flex items-center gap-1.5 border-b px-3.5 py-3 text-[12px] font-medium transition-colors",
              activeTab === t.id
                ? "border-text text-text"
                : "border-transparent text-text-muted hover:text-text",
            )}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      <form ref={formRef} onSubmit={submit} className="p-4">
        <input type="hidden" name="kind" value={activeTab} />

        {activeTab === "file" ? (
          <label
            onDragEnter={(e) => {
              e.preventDefault();
              setDragActive(true);
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragActive(true);
            }}
            onDragLeave={() => setDragActive(false)}
            onDrop={onDrop}
            className={cn(
              "relative flex cursor-pointer flex-col items-center justify-center gap-2.5 rounded-lg border border-dashed border-border-strong bg-bg px-6 py-8 text-center transition-all",
              dragActive && "border-accent-border bg-accent-muted",
              fileName && !dragActive && "border-accent-border/60",
            )}
          >
            <input
              ref={fileInputRef}
              name="file"
              type="file"
              accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              onChange={onFileChange}
              className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
            />
            <div className="flex h-9 w-9 items-center justify-center rounded-[8px] border border-border bg-bg-elevated text-text-muted">
              {fileName ? (
                <FileText className="h-[18px] w-[18px]" />
              ) : (
                <Upload className="h-[18px] w-[18px]" />
              )}
            </div>
            {fileName ? (
              <>
                <div className="font-mono text-[12.5px] text-text">
                  {fileName}
                </div>
              </>
            ) : (
              <>
                <div className="text-[13px] font-medium">
                  {m.position.upload.dropTitle}
                </div>
                <div className="font-mono text-[11px] tracking-[0.02em] text-text-faint">
                  {m.position.upload.dropSub}
                </div>
              </>
            )}
          </label>
        ) : null}

        {activeTab === "text" ? (
          <textarea
            name="raw_text"
            rows={9}
            minLength={50}
            placeholder={m.position.upload.pasteTextPlaceholder}
            className="block min-h-[180px] w-full resize-y rounded-[6px] border border-border bg-bg p-[11px] text-[13px] outline-none transition-colors focus:border-border-strong"
          />
        ) : null}

        {activeTab === "hh" ? (
          <textarea
            name="hh_json"
            rows={11}
            placeholder={m.position.upload.pasteHhPlaceholder}
            className="block min-h-[220px] w-full resize-y rounded-[6px] border border-border bg-bg p-[11px] font-mono text-[12px] outline-none transition-colors focus:border-border-strong"
          />
        ) : null}

        <div className="mt-3 flex items-center justify-between">
          <span className="font-mono text-[11px] text-text-faint">
            {m.position.upload.autoScored}
          </span>
          <button
            type="submit"
            disabled={pending || (activeTab === "file" && !fileName)}
            className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[11.5px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {pending
              ? m.position.upload.scoring
              : m.position.upload.uploadAndScore}
          </button>
        </div>

        {error ? (
          <p className="mt-2 text-[11px] text-score-low">{error}</p>
        ) : null}
      </form>
    </div>
  );
}
