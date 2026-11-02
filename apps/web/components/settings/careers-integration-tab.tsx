"use client";

import { useMemo, useState } from "react";
import {
  Check,
  Code2,
  Copy,
  ExternalLink,
  Eye,
  Globe2,
  ShieldCheck,
} from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import type { Organization } from "@/lib/types";

export function CareersIntegrationTab({
  organization,
  apiUrl,
}: {
  organization: Organization | null;
  apiUrl: string | null;
}) {
  const { locale, m } = useI18n();
  const t = m.settings;
  const [copied, setCopied] = useState<"url" | "code" | null>(null);

  const code = useMemo(
    () => {
      if (!apiUrl || !organization) return "";
      const apiBase = apiUrl.split("/public/v1/careers/")[0];
      const widgetUrl = `${apiBase}/public/v1/careers/widget.js`;
      return (
        `<div data-recruiter-careers\n` +
        `  data-organization="${organization.slug}"\n` +
        `  data-locale="${locale}"\n` +
        `  data-source="company_website"></div>\n` +
        `<script async src="${widgetUrl}"></script>`
      );
    },
    [apiUrl, locale, organization],
  );
  const previewUrl = useMemo(() => {
    if (!apiUrl) return "";
    return `${apiUrl.replace(/\/positions\/?$/, "")}/preview?locale=${locale}`;
  }, [apiUrl, locale]);

  if (!organization || !apiUrl) {
    return (
      <p className="text-[12.5px] text-text-muted">{t.careersNotAdmin}</p>
    );
  }

  async function copy(value: string, kind: "url" | "code") {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(kind);
      toast.success(t.careersCopiedToast);
      window.setTimeout(() => setCopied(null), 1800);
    } catch {
      toast.error(t.careersCopyFailedToast);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 className="text-[15px] font-semibold tracking-tight">
          {t.careersTitle}
        </h2>
        <p className="max-w-3xl text-[12.5px] leading-relaxed text-text-muted">
          {t.careersDesc}
        </p>
      </header>

      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <div className="mb-3 flex items-center gap-2">
          <Globe2 className="h-4 w-4 text-primary" />
          <h3 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-text-muted">
            {t.careersEndpointLabel}
          </h3>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row">
          <code className="min-w-0 flex-1 overflow-x-auto rounded-md border border-border bg-bg px-3 py-2.5 font-mono text-[11px] text-text-muted">
            {apiUrl}
          </code>
          <button
            type="button"
            onClick={() => copy(apiUrl, "url")}
            className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border bg-bg px-3 py-2 text-[12px] font-medium transition-colors hover:border-border-strong hover:bg-bg-hover"
          >
            {copied === "url" ? (
              <Check className="h-3.5 w-3.5 text-primary" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
            {copied === "url" ? t.careersCopied : t.careersCopy}
          </button>
          <a
            href={previewUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border bg-bg px-3 py-2 text-[12px] font-medium transition-colors hover:border-border-strong hover:bg-bg-hover"
          >
            <Eye className="h-3.5 w-3.5" />
            {t.careersPreview}
          </a>
          <a
            href={apiUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center justify-center gap-1.5 rounded-md bg-text px-3 py-2 text-[12px] font-medium text-bg transition-opacity hover:opacity-90"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            {t.careersOpenJson}
          </a>
        </div>
        <p className="mt-3 text-[11.5px] leading-relaxed text-text-faint">
          {t.careersEndpointHint(organization.slug)}
        </p>
      </section>

      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <div className="mb-3 flex items-center gap-2">
          <Code2 className="h-4 w-4 text-primary" />
          <h3 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-text-muted">
            {t.careersCodeLabel}
          </h3>
        </div>
        <div className="relative">
          <pre className="overflow-x-auto rounded-lg border border-border bg-bg p-4 pr-12 font-mono text-[11px] leading-relaxed text-text-muted">
            <code>{code}</code>
          </pre>
          <button
            type="button"
            aria-label={t.careersCopyCode}
            onClick={() => copy(code, "code")}
            className="absolute right-2 top-2 rounded-md border border-border bg-bg-elevated p-2 text-text-muted transition-colors hover:text-text"
          >
            {copied === "code" ? (
              <Check className="h-3.5 w-3.5 text-primary" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
      </section>

      <section className="flex gap-3 rounded-xl border border-primary/20 bg-primary/[0.04] p-5">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        <div className="flex flex-col gap-1">
          <h3 className="text-[12.5px] font-medium">{t.careersSecurityTitle}</h3>
          <p className="text-[11.5px] leading-relaxed text-text-muted">
            {t.careersSecurityDesc}
          </p>
        </div>
      </section>
    </div>
  );
}
