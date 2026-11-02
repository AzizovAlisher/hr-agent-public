"use client";

import Link from "next/link";
import { Check, ChevronRight, Loader2, ShieldCheck, ShieldX } from "lucide-react";
import { useState, useTransition } from "react";

import { decideAiActionBundleAction } from "@/app/ai/actions";
import { BundleStatusBadge } from "@/components/ai/ai-status";
import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import { JsonView } from "@/components/ui/json-view";
import { getAiCopy } from "@/lib/ai/copy";
import type { AiActionBundle } from "@/lib/ai/types";

export function ApprovalInbox({ initialBundles }: { initialBundles: AiActionBundle[] }) {
  const { locale } = useI18n();
  const copy = getAiCopy(locale);
  const [bundles, setBundles] = useState(initialBundles);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  function decide(bundle: AiActionBundle, decision: "approve" | "reject") {
    setPendingId(bundle.id);
    setError(null);
    startTransition(async () => {
      const result = await decideAiActionBundleAction(bundle.id, decision);
      if (result.ok) {
        setBundles((current) => current.map((item) => item.id === bundle.id ? result.data : item));
      } else {
        setError(result.error);
      }
      setPendingId(null);
    });
  }

  const pending = bundles.filter((bundle) => bundle.status === "pending");
  const history = bundles.filter((bundle) => bundle.status !== "pending");
  if (!bundles.length) {
    return (
      <div className="mx-auto flex max-w-md flex-col items-center py-20 text-center">
        <span className="flex h-11 w-11 items-center justify-center rounded-xl border border-border bg-bg-elevated text-score-high"><ShieldCheck className="h-5 w-5" /></span>
        <h2 className="mt-4 text-[15px] font-semibold">{copy.empty.approvalsTitle}</h2>
        <p className="mt-1.5 text-[12px] leading-5 text-text-muted">{copy.empty.approvalsBody}</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {error ? <div className="rounded-xl border border-score-low/25 bg-score-low/5 p-3 text-[11px] text-score-low">{error}</div> : null}
      {pending.length ? (
        <section className="space-y-3">
          {pending.map((bundle) => (
            <ApprovalCard key={bundle.id} bundle={bundle} locale={locale} copy={copy} busy={pendingId === bundle.id} onDecide={decide} />
          ))}
        </section>
      ) : (
        <div className="rounded-xl border border-score-high/25 bg-score-high/5 px-4 py-5 text-center text-[12px] text-score-high"><Check className="mx-auto mb-2 h-4 w-4" />{copy.empty.approvalsTitle}</div>
      )}
      {history.length ? (
        <section>
          <h2 className="mb-3 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">{locale === "ru" ? "История" : "History"}</h2>
          <div className="space-y-2">
            {history.map((bundle) => <ApprovalCard key={bundle.id} bundle={bundle} locale={locale} copy={copy} busy={false} onDecide={decide} />)}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function ApprovalCard({ bundle, locale, copy, busy, onDecide }: { bundle: AiActionBundle; locale: "en" | "ru"; copy: ReturnType<typeof getAiCopy>; busy: boolean; onDecide: (bundle: AiActionBundle, decision: "approve" | "reject") => void }) {
  const actionable = bundle.status === "pending";
  return (
    <article className="overflow-hidden rounded-xl border border-border bg-bg-elevated">
      <div className="p-4 sm:p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 gap-3">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-score-mid/10 text-score-mid"><ShieldCheck className="h-4 w-4" /></span>
            <div className="min-w-0"><h2 className="text-[13px] font-semibold">{bundle.summary}</h2><div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[9.5px] text-text-faint"><span>{copy.approval.permission}: <span className="font-mono">{bundle.required_permission}</span></span><span>{copy.approval.planVersion}: {bundle.plan_version}</span></div></div>
          </div>
          <BundleStatusBadge status={bundle.status} locale={locale} />
        </div>
        <div className="mt-4 rounded-lg border border-border bg-background px-3 py-2.5">
          <div className="mb-2 text-[9px] font-medium uppercase tracking-[0.06em] text-text-faint">{copy.approval.preview}</div>
          <JsonView value={bundle.preview} />
        </div>
        {bundle.status === "stale" ? <p className="mt-3 text-[10.5px] text-score-mid">{copy.approval.stale}</p> : null}
        {bundle.error_code ? <p className="mt-3 text-[10.5px] text-score-low">{bundle.error_code}</p> : null}
      </div>
      <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border bg-bg-hover/40 px-4 py-3 sm:px-5">
        <Link href={`/ai/missions/${bundle.mission_id}`} className="inline-flex items-center gap-1 text-[10.5px] text-text-muted hover:text-text">{locale === "ru" ? "Открыть миссию" : "Open mission"}<ChevronRight className="h-3 w-3" /></Link>
        {actionable ? <div className="flex items-center gap-2"><Button variant="outline" size="sm" disabled={busy} onClick={() => onDecide(bundle, "reject")}><ShieldX />{busy ? copy.approval.rejecting : copy.approval.reject}</Button><Button size="sm" disabled={busy} onClick={() => onDecide(bundle, "approve")}>{busy ? <Loader2 className="animate-spin" /> : <ShieldCheck />}{busy ? copy.approval.approving : copy.approval.approve}</Button></div> : null}
      </footer>
    </article>
  );
}
