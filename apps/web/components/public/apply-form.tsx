"use client";

import { useEffect, useRef, useState, useTransition } from "react";
import { Check, FileText, Upload } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import { submitApplication } from "@/lib/public-actions";
import { cn } from "@/lib/utils";

const ACCEPT = ".pdf,.docx";
const MAX_BYTES = 15 * 1024 * 1024;

/** @ui-only Public-application input mode. */
type Mode = "file" | "text";
/** @ui-only Public-application client validation target. */
type InvalidField = "name" | "phone" | "email" | "cv" | "text" | "privacy" | null;

const PRIVACY_NOTICE_VERSION = "2026-08-24";

export function ApplyForm({
  positionId,
  controllerName,
  utmSource,
  referralToken,
}: {
  positionId: string;
  controllerName: string;
  utmSource: string | null;
  referralToken: string | null;
}) {
  const { m } = useI18n();
  const t = m.apply;

  const [mode, setMode] = useState<Mode>("file");
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("+992 ");
  const [email, setEmail] = useState("");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [invalidField, setInvalidField] = useState<InvalidField>(null);
  const [done, setDone] = useState(false);
  const [privacyAccepted, setPrivacyAccepted] = useState(false);
  const [pending, startSubmit] = useTransition();
  const fileRef = useRef<HTMLInputElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const phoneRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const uploadButtonRef = useRef<HTMLButtonElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const privacyRef = useRef<HTMLInputElement>(null);
  const idempotencyKey = useRef<string | null>(null);

  useEffect(() => {
    if (error && invalidField === null) errorRef.current?.focus();
  }, [error, invalidField]);

  function reject(message: string, field: Exclude<InvalidField, null>) {
    setError(message);
    setInvalidField(field);
    const target =
      field === "name"
        ? nameRef.current
        : field === "phone"
          ? phoneRef.current
          : field === "email"
            ? emailRef.current
          : field === "text"
            ? textRef.current
            : field === "privacy"
              ? privacyRef.current
              : uploadButtonRef.current;
    target?.focus();
  }

  function pickFile() {
    fileRef.current?.click();
  }

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > MAX_BYTES) {
      reject(t.errors.fileTooBig, "cv");
      return;
    }
    setError(null);
    setInvalidField(null);
    setFile(f);
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (pending) return;
    setError(null);
    setInvalidField(null);

    if (!name.trim()) {
      reject(t.errors.nameRequired, "name");
      return;
    }
    if (phone.replace(/\D/g, "").length < 7) {
      reject(t.errors.phoneRequired, "phone");
      return;
    }
    if (email.trim() && !emailRef.current?.checkValidity()) {
      reject(t.errors.emailInvalid, "email");
      return;
    }
    if (mode === "file" && !file) {
      reject(t.errors.cvRequired, "cv");
      return;
    }
    if (mode === "text" && text.trim().length < 50) {
      reject(t.errors.textTooShort, "text");
      return;
    }
    if (!privacyAccepted) {
      reject(t.errors.privacyRequired, "privacy");
      return;
    }

    const fd = new FormData();
    fd.set("name", name.trim());
    fd.set("phone", phone.trim());
    if (email.trim()) fd.set("email", email.trim());
    if (utmSource) fd.set("source", utmSource);
    if (referralToken) fd.set("referral_token", referralToken);
    if (mode === "file" && file) fd.set("file", file);
    if (mode === "text") fd.set("raw_text", text);
    idempotencyKey.current ??= crypto.randomUUID();
    fd.set("idempotency_key", idempotencyKey.current);
    fd.set("privacy_notice_version", PRIVACY_NOTICE_VERSION);
    fd.set("privacy_acknowledged", "true");

    startSubmit(async () => {
      const res = await submitApplication(positionId, fd);
      if (!res.ok) {
        // Public intake failures stay generic so contact identity cannot be
        // inferred by comparing responses.
        setError(t.errors.serverError);
        setInvalidField(null);
        return;
      }
      setDone(true);
    });
  }

  if (done) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-lg border border-accent-border bg-accent-muted px-6 py-8 text-center">
        <span className="flex h-10 w-10 items-center justify-center rounded-full bg-primary text-primary-foreground">
          <Check className="h-5 w-5" />
        </span>
        <h3 className="text-[15px] font-semibold text-text">{t.doneTitle}</h3>
        <p className="max-w-[360px] text-[12.5px] leading-[1.55] text-text-muted">
          {t.doneDesc}
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <Field label={t.fields.name} required>
        <input
          ref={nameRef}
          value={name}
          required
          onChange={(e) => {
            setName(e.target.value);
            if (invalidField === "name") setInvalidField(null);
          }}
          aria-invalid={invalidField === "name"}
          aria-describedby={invalidField === "name" ? "apply-form-error" : undefined}
          autoComplete="name"
          placeholder={t.fields.namePlaceholder}
          className="block w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[13px] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
        />
      </Field>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label={t.fields.phone} required hint={t.fields.phoneHint}>
          <input
            ref={phoneRef}
            value={phone}
            required
            onChange={(e) => {
              setPhone(e.target.value);
              if (invalidField === "phone") setInvalidField(null);
            }}
            aria-invalid={invalidField === "phone"}
            aria-describedby={invalidField === "phone" ? "apply-form-error" : undefined}
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            placeholder={t.fields.phonePlaceholder}
            className="block w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[13px] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
          />
        </Field>
        <Field label={t.fields.email} hint={t.fields.emailHint}>
          <input
            ref={emailRef}
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
              if (invalidField === "email") setInvalidField(null);
            }}
            type="email"
            aria-invalid={invalidField === "email"}
            aria-describedby={invalidField === "email" ? "apply-form-error" : undefined}
            inputMode="email"
            autoComplete="email"
            placeholder={t.fields.emailPlaceholder}
            className="block w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[13px] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
          />
        </Field>
      </div>

      {/* CV input — file (default) or paste text */}
      <div>
        <div className="mb-2 flex items-center gap-2">
          <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {t.fields.cvLabel}
          </span>
          <span className="text-text-faint">·</span>
          <button
            type="button"
            onClick={() => {
              setMode("file");
              setError(null);
              setInvalidField(null);
            }}
            className={cn(
              "inline-flex min-h-6 min-w-6 items-center px-1 text-[11px] transition-colors",
              mode === "file"
                ? "font-medium text-text"
                : "text-text-muted hover:text-text",
            )}
          >
            {t.fields.cvFile}
          </button>
          <span className="text-text-faint">/</span>
          <button
            type="button"
            onClick={() => {
              setMode("text");
              setError(null);
              setInvalidField(null);
            }}
            className={cn(
              "inline-flex min-h-6 min-w-6 items-center px-1 text-[11px] transition-colors",
              mode === "text"
                ? "font-medium text-text"
                : "text-text-muted hover:text-text",
            )}
          >
            {t.fields.cvText}
          </button>
        </div>

        {mode === "file" ? (
          <>
            <button
              ref={uploadButtonRef}
              type="button"
              onClick={pickFile}
              aria-invalid={invalidField === "cv"}
              aria-describedby={invalidField === "cv" ? "apply-form-error" : undefined}
              className="group flex w-full items-center gap-3 rounded-lg border border-dashed border-border bg-bg/40 px-4 py-4 text-left transition-colors hover:border-accent-border hover:bg-accent-muted"
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
                    {t.fields.cvReplaceHint((file.size / 1024).toFixed(0))}
                  </span>
                </>
              ) : (
                <>
                  <span className="text-[13px] font-medium text-text">
                    {t.fields.cvUpload}
                  </span>
                  <span className="text-[11px] text-text-muted">
                    {t.fields.cvUploadHint}
                  </span>
                </>
              )}
            </span>
            </button>
            <input
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              onChange={onFile}
              tabIndex={-1}
              aria-hidden="true"
              className="hidden"
            />
          </>
        ) : (
          <textarea
            ref={textRef}
            value={text}
            required
            onChange={(e) => {
              setText(e.target.value);
              if (invalidField === "text") setInvalidField(null);
            }}
            aria-invalid={invalidField === "text"}
            aria-describedby={invalidField === "text" ? "apply-form-error" : undefined}
            rows={10}
            placeholder={t.fields.cvTextPlaceholder}
            className="block w-full resize-y rounded-[6px] border border-border bg-bg px-3 py-2 text-[13px] leading-[1.55] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
          />
        )}
      </div>

      {error ? (
        <div
          ref={errorRef}
          id="apply-form-error"
          role="alert"
          tabIndex={-1}
          className="rounded-[6px] border border-score-low/30 bg-score-low/10 px-3 py-2 text-[12px] text-score-low"
        >
          {error}
        </div>
      ) : null}

      <details className="rounded-lg border border-border bg-bg/40 px-3 py-2 text-[11px] leading-[1.55] text-text-muted">
        <summary className="cursor-pointer font-medium text-text">{t.privacySummary}</summary>
        <ul className="mt-2 list-disc space-y-1 pl-4">
          <li>{t.privacyController(controllerName)}</li>
          <li>{t.privacyPurpose}</li>
          <li>{t.privacyAutomation}</li>
          <li>{t.privacyTransfer}</li>
          <li>{t.privacyRetention}</li>
          <li>{t.privacyRights}</li>
        </ul>
      </details>

      <div className="flex items-center justify-between gap-3 pt-2">
        <label className="flex min-h-11 items-center gap-2 text-[10.5px] text-text-muted">
          <input
            ref={privacyRef}
            type="checkbox"
            checked={privacyAccepted}
            onChange={(event) => {
              setPrivacyAccepted(event.target.checked);
              if (invalidField === "privacy") setInvalidField(null);
            }}
            aria-invalid={invalidField === "privacy"}
            aria-describedby={invalidField === "privacy" ? "apply-form-error" : undefined}
            className="h-4 w-4"
          />
          <span>{t.consent}</span>
        </label>
        <button
          type="submit"
          disabled={pending}
          className="inline-flex shrink-0 items-center gap-2 rounded-[6px] bg-text px-4 py-2 text-[13px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          {pending ? t.submitting : t.submit}
        </button>
      </div>
    </form>
  );
}

function Field({
  label,
  hint,
  required,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
        {label}
        {required ? <span className="text-score-low">*</span> : null}
        {hint ? (
          <span className="font-mono text-[10px] normal-case tracking-normal text-text-faint">
            · {hint}
          </span>
        ) : null}
      </span>
      {children}
    </label>
  );
}
