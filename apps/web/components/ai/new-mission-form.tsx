"use client";

import { useRouter } from "next/navigation";
import { ArrowUp, FileSpreadsheet, FileText, Loader2, Paperclip, ShieldCheck, Sparkles, X } from "lucide-react";
import { useRef, useState, type FormEvent } from "react";

import { createAiSessionAction } from "@/app/ai/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import { getAiCopy } from "@/lib/ai/copy";
import { normalizeAiAttachmentUpload } from "@/lib/ai/normalize";
import { consumeAiEventStream } from "@/lib/ai/stream";
import { cn } from "@/lib/utils";

const MAX_FILES = 10;

export function NewMissionForm() {
  const { locale } = useI18n();
  const copy = getAiCopy(locale);
  const router = useRouter();
  const fileInput = useRef<HTMLInputElement>(null);
  const [request, setRequest] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function addFiles(next: FileList | null) {
    if (!next) return;
    setFiles((current) => [...current, ...Array.from(next)].slice(0, MAX_FILES));
    if (fileInput.current) fileInput.current.value = "";
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const content = request.trim();
    if (!content || pending) return;
    setPending(true);
    setError(null);

    const created = await createAiSessionAction({ title: content.slice(0, 80) });
    if (!created.ok) {
      setError(created.error);
      setPending(false);
      return;
    }

    const sessionId = created.data.id;
    try {
      const attachments: { id: string; upload_token: string }[] = [];
      for (const file of files) {
        const formData = new FormData();
        formData.append("file", file);
        const upload = await fetch(`/api/ai/sessions/${sessionId}/attachments`, {
          method: "POST",
          body: formData,
        });
        if (!upload.ok) throw new Error(await upload.text());
        const uploaded = normalizeAiAttachmentUpload(await upload.json());
        attachments.push({ id: uploaded.id, upload_token: uploaded.upload_token });
      }

      let missionId: string | undefined;
      const response = await fetch(`/api/ai/sessions/${sessionId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, attachments }),
      });
      await consumeAiEventStream(response, (streamEvent) => {
        if (streamEvent.type === "mission") missionId = streamEvent.mission.id;
        if (streamEvent.type === "done" && streamEvent.mission_id) missionId = streamEvent.mission_id;
      });
      router.push(missionId ? `/ai/missions/${missionId}` : `/ai/sessions/${sessionId}`);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : copy.common.unavailable);
      setPending(false);
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col items-center px-5 py-10 sm:px-8 sm:py-16">
      <div className="mb-7 flex h-12 w-12 items-center justify-center rounded-2xl border border-accent-border bg-accent-muted text-primary shadow-[0_0_40px_var(--accent-muted)]">
        <Sparkles className="h-5 w-5" />
      </div>
      <h1 className="text-center text-[28px] font-semibold tracking-[-0.035em] sm:text-[34px]">
        {copy.page.newTitle}
      </h1>
      <p className="mt-3 max-w-2xl text-center text-[13px] leading-6 text-text-muted sm:text-[14px]">
        {copy.page.newSubtitle}
      </p>

      <form onSubmit={submit} className="mt-9 w-full">
        <div className={cn(
          "overflow-hidden rounded-2xl border bg-bg-elevated shadow-[0_18px_70px_rgba(0,0,0,0.18)] transition-colors",
          error ? "border-score-low/50" : "border-border focus-within:border-accent-border",
        )}>
          <textarea
            value={request}
            onChange={(event) => setRequest(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            placeholder={copy.page.promptPlaceholder}
            disabled={pending}
            rows={5}
            className="w-full resize-none bg-transparent px-5 pb-3 pt-5 text-[14px] leading-6 outline-none placeholder:text-text-faint disabled:opacity-60"
          />

          {files.length ? (
            <div className="flex flex-wrap gap-2 px-4 pb-3">
              {files.map((file, index) => (
                <span key={`${file.name}-${index}`} className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-border bg-bg-hover px-2 py-1 text-[11px] text-text-muted">
                  {file.name.endsWith(".csv") || file.name.endsWith(".xlsx") ? (
                    <FileSpreadsheet className="h-3 w-3 text-score-high" />
                  ) : (
                    <FileText className="h-3 w-3 text-primary" />
                  )}
                  <span className="max-w-[220px] truncate">{file.name}</span>
                  <button
                    type="button"
                    disabled={pending}
                    onClick={() => setFiles((current) => current.filter((_, i) => i !== index))}
                    className="rounded text-text-faint hover:text-text"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          ) : null}

          <div className="flex items-center justify-between gap-3 border-t border-border px-3 py-3">
            <div className="flex min-w-0 items-center gap-2">
              <input
                ref={fileInput}
                type="file"
                multiple
                accept=".pdf,.docx,.txt,.csv,.xlsx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                className="hidden"
                onChange={(event) => addFiles(event.target.files)}
              />
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={pending || files.length >= MAX_FILES}
                onClick={() => fileInput.current?.click()}
              >
                <Paperclip className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">{copy.page.attachments}</span>
              </Button>
              <span className="hidden text-[10px] text-text-faint md:inline">{copy.page.attachmentHint}</span>
            </div>
            <Button type="submit" disabled={!request.trim() || pending} className="rounded-xl">
              {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowUp className="h-4 w-4" />}
              <span>{pending ? copy.page.starting : copy.page.start}</span>
            </Button>
          </div>
        </div>
        {error ? <p className="mt-3 text-center text-[12px] text-score-low">{error}</p> : null}
      </form>

      <div className="mt-5 flex items-center gap-1.5 text-[10.5px] text-text-faint">
        <ShieldCheck className="h-3 w-3 text-score-high" />
        {copy.page.privacy}
      </div>

      <div className="mt-9 w-full border-t border-border pt-6">
        <div className="mb-3 text-center text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {copy.page.examples}
        </div>
        <div className="grid gap-2 md:grid-cols-3">
          {copy.examples.map((example) => (
            <button
              key={example}
              type="button"
              disabled={pending}
              onClick={() => setRequest(example)}
              className="rounded-xl border border-border bg-bg-elevated px-3 py-3 text-left text-[11.5px] leading-5 text-text-muted transition-colors hover:border-accent-border hover:bg-bg-hover hover:text-text"
            >
              {example}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
