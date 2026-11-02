"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertCircle,
  ArrowLeft,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleStop,
  ExternalLink,
  FileSearch,
  Loader2,
  MessageSquare,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldAlert,
  Sparkles,
  Target,
  User,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, useTransition, type FormEvent, type ReactNode } from "react";

import {
  replanHiringMissionAction,
  retryHiringMissionAction,
  stopHiringMissionAction,
} from "@/app/ai/actions";
import { MissionStatusBadge, StepStatus } from "@/components/ai/ai-status";
import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import { getAiCopy } from "@/lib/ai/copy";
import { consumeAiEventStream } from "@/lib/ai/stream";
import type {
  AiActionBundle,
  AiMessage,
  AiSessionDetail,
  EvidenceClaim,
  HiringMission,
  MissionCandidateAssessment,
  MissionEvent,
} from "@/lib/ai/types";
import { cn } from "@/lib/utils";

/** @ui-only Mission-canvas tab state. */
type CanvasTab = "overview" | "plan" | "candidates" | "evidence" | "activity";

export function MissionWorkspace({
  initialSession,
  initialMission,
  initialCandidates,
  initialEvents,
  initialBundles,
  partialFailure = false,
}: {
  initialSession: AiSessionDetail;
  initialMission: HiringMission | null;
  initialCandidates: MissionCandidateAssessment[];
  initialEvents: MissionEvent[];
  initialBundles: AiActionBundle[];
  partialFailure?: boolean;
}) {
  const { locale } = useI18n();
  const copy = getAiCopy(locale);
  const router = useRouter();
  const endRef = useRef<HTMLDivElement>(null);
  const [messages, setMessages] = useState(initialSession.messages ?? []);
  const [mission, setMission] = useState(initialMission);
  const [canvasTab, setCanvasTab] = useState<CanvasTab>("overview");
  const [mobileView, setMobileView] = useState<"chat" | "canvas">("canvas");
  const [composer, setComposer] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [controlPending, startControl] = useTransition();
  const [expandedCandidate, setExpandedCandidate] = useState<string | null>(
    initialCandidates[0]?.candidate_id ?? null,
  );

  // Mission work is performed by a durable worker after the request stream
  // closes. Refresh the server-rendered canvas while it is active, and fold
  // the refreshed mission/messages back into local chat state without
  // disturbing the user's composer.
  useEffect(() => {
    setMission(initialMission);
  }, [initialMission]);

  useEffect(() => {
    setMessages(initialSession.messages ?? []);
  }, [initialSession.messages]);

  useEffect(() => {
    if (!mission || !["planning", "running"].includes(mission.status)) return;
    const interval = window.setInterval(() => router.refresh(), 2_000);
    return () => window.clearInterval(interval);
  }, [mission, router]);

  const pendingBundles = initialBundles.filter((bundle) => bundle.status === "pending");
  const orderedCandidates = useMemo(() => [...initialCandidates].sort((a, b) => {
    if (b.overall_score !== a.overall_score) return b.overall_score - a.overall_score;
    if (b.evidence_coverage !== a.evidence_coverage) return b.evidence_coverage - a.evidence_coverage;
    const confidence = { high: 3, medium: 2, low: 1 };
    if (confidence[b.confidence] !== confidence[a.confidence]) return confidence[b.confidence] - confidence[a.confidence];
    return a.candidate_id.localeCompare(b.candidate_id);
  }), [initialCandidates]);

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const content = composer.trim();
    if (!content || streaming) return;
    const optimisticId = `optimistic-${Date.now()}`;
    const assistantId = `stream-${Date.now()}`;
    const now = new Date().toISOString();
    const optimistic: AiMessage = {
      id: optimisticId,
      session_id: initialSession.id,
      sequence: messages.length + 1,
      role: "user",
      status: "completed",
      content,
      structured_content: null,
      attachments: [],
      created_at: now,
    };
    setMessages((current) => [...current, optimistic]);
    setComposer("");
    setStreaming(true);
    setStreamError(null);
    queueMicrotask(() => endRef.current?.scrollIntoView({ behavior: "smooth" }));

    try {
      const response = await fetch(`/api/ai/sessions/${initialSession.id}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      await consumeAiEventStream(response, (streamEvent) => {
        if (streamEvent.type === "message") {
          setMessages((current) => {
            const withoutStream = current.filter((message) =>
              message.id !== assistantId &&
              !(
                message.id === optimisticId &&
                streamEvent.message.role === "user" &&
                message.content === streamEvent.message.content
              ),
            );
            const index = withoutStream.findIndex((message) => message.id === streamEvent.message.id);
            if (index >= 0) {
              const next = [...withoutStream];
              next[index] = streamEvent.message;
              return next;
            }
            return [...withoutStream, streamEvent.message];
          });
        }
        if (streamEvent.type === "message_delta") {
          setMessages((current) => {
            const existing = current.find((message) => message.id === assistantId);
            if (existing) {
              return current.map((message) => message.id === assistantId
                ? { ...message, content: message.content + streamEvent.delta }
                : message);
            }
            return [...current, {
              id: assistantId,
              session_id: initialSession.id,
              sequence: current.length + 1,
              role: "assistant",
              status: "streaming",
              content: streamEvent.delta,
              structured_content: null,
              attachments: [],
              created_at: new Date().toISOString(),
            }];
          });
        }
        if (streamEvent.type === "mission") {
          setMission(streamEvent.mission);
          if (!initialMission) router.replace(`/ai/missions/${streamEvent.mission.id}`);
        }
        if (streamEvent.type === "error") setStreamError(streamEvent.message);
      });
      setMessages((current) => current.map((message) =>
        message.id === assistantId ? { ...message, status: "completed" } : message,
      ));
      router.refresh();
    } catch (cause) {
      setStreamError(cause instanceof Error ? cause.message : copy.mission.failedToSend);
    } finally {
      setStreaming(false);
      queueMicrotask(() => endRef.current?.scrollIntoView({ behavior: "smooth" }));
    }
  }

  function runControl(kind: "stop" | "retry" | "replan") {
    if (!mission) return;
    startControl(async () => {
      const result = kind === "stop"
        ? await stopHiringMissionAction(mission.id)
        : kind === "retry"
          ? await retryHiringMissionAction(mission.id)
          : await replanHiringMissionAction(mission.id, mission.original_request);
      if (result.ok) setMission(result.data);
      else setStreamError(result.error);
    });
  }

  const canvasTabs: { key: CanvasTab; label: string }[] = [
    { key: "overview", label: copy.mission.overview },
    { key: "plan", label: copy.mission.plan },
    { key: "candidates", label: copy.mission.candidates },
    { key: "evidence", label: copy.mission.evidence },
    { key: "activity", label: copy.mission.activity },
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex min-h-[62px] shrink-0 items-center gap-3 border-b border-border px-4 sm:px-5">
        <Link href="/ai/active" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-text-muted hover:bg-bg-hover hover:text-text">
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <h1 className="truncate text-[14px] font-semibold">{mission?.title || initialSession.title || copy.common.noTitle}</h1>
            {mission ? <MissionStatusBadge status={mission.status} locale={locale} /> : null}
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[10px] text-text-faint">
            <span>{copy.mission.planVersion} {mission?.current_plan_version ?? 0}</span>
            {mission?.candidate_count != null ? <><span>·</span><span>{mission.candidate_count} {copy.common.candidates}</span></> : null}
          </div>
        </div>
        {mission ? (
          <div className="hidden items-center gap-1 sm:flex">
            {mission.position_id ? (
              <Button render={<Link href={`/positions/${mission.position_id}`} />} variant="outline" size="sm">
                <ExternalLink className="h-3.5 w-3.5" />{copy.mission.openHuman}
              </Button>
            ) : null}
            {mission.status === "failed" ? (
              <Button variant="outline" size="sm" disabled={controlPending} onClick={() => runControl("retry")}><RefreshCw />{copy.mission.retry}</Button>
            ) : null}
            {!["completed", "stopped"].includes(mission.status) ? (
              <Button variant="ghost" size="sm" disabled={controlPending} onClick={() => runControl("stop")}><CircleStop />{copy.mission.stop}</Button>
            ) : null}
          </div>
        ) : null}
      </header>

      <div className="flex border-b border-border md:hidden">
        {(["chat", "canvas"] as const).map((view) => (
          <button key={view} type="button" onClick={() => setMobileView(view)} className={cn("flex-1 px-3 py-2 text-[11px] font-medium", mobileView === view ? "border-b-2 border-primary text-primary" : "text-text-muted")}>
            {view === "chat" ? copy.mission.chat : copy.mission.overview}
          </button>
        ))}
      </div>

      <div className="flex min-h-0 flex-1">
        <section className={cn("min-h-0 w-full shrink-0 flex-col border-r border-border bg-bg-elevated/35 md:flex md:w-[390px] xl:w-[440px]", mobileView === "chat" ? "flex" : "hidden")}>
          <div className="flex items-center gap-2 border-b border-border px-4 py-3 text-[11px] font-medium text-text-muted">
            <Bot className="h-3.5 w-3.5 text-primary" />
            {copy.mission.chat}
            <span className="ml-auto inline-flex items-center gap-1 text-[9px] text-score-high"><span className="h-1.5 w-1.5 rounded-full bg-score-high" />{copy.assistantOnline}</span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
            {messages.length ? (
              <div className="space-y-4">
                {messages.filter((message) => message.role === "user" || message.role === "assistant").map((message) => (
                  <MessageBubble key={message.id} message={message} />
                ))}
              </div>
            ) : (
              <div className="flex h-full min-h-52 flex-col items-center justify-center px-6 text-center">
                <Bot className="h-6 w-6 text-primary" />
                <p className="mt-3 text-[12px] leading-5 text-text-muted">{copy.empty.messages}</p>
              </div>
            )}
            {streaming && !messages.some((message) => message.status === "streaming") ? (
              <div className="mt-4 flex items-center gap-2 text-[11px] text-text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />{copy.mission.thinking}</div>
            ) : null}
            <div ref={endRef} />
          </div>
          <form onSubmit={sendMessage} className="border-t border-border p-3">
            {streamError ? <p className="mb-2 text-[10.5px] text-score-low">{streamError}</p> : null}
            <div className="flex items-end gap-2 rounded-xl border border-border bg-background p-2 focus-within:border-accent-border">
              <textarea value={composer} onChange={(event) => setComposer(event.target.value)} rows={2} disabled={streaming} placeholder={copy.mission.ask} className="max-h-36 min-h-10 flex-1 resize-none bg-transparent px-1 py-1 text-[12px] leading-5 outline-none placeholder:text-text-faint" />
              <Button type="submit" size="icon-sm" disabled={!composer.trim() || streaming} aria-label={copy.mission.send}>{streaming ? <Loader2 className="animate-spin" /> : <Send />}</Button>
            </div>
          </form>
        </section>

        <section className={cn("min-h-0 min-w-0 flex-1 flex-col", mobileView === "canvas" ? "flex" : "hidden md:flex")}>
          <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-border px-3 pt-2 sm:px-5">
            {canvasTabs.map((tab) => (
              <button key={tab.key} type="button" onClick={() => setCanvasTab(tab.key)} className={cn("whitespace-nowrap border-b-2 px-2.5 pb-2 pt-1 text-[11px] font-medium transition-colors", canvasTab === tab.key ? "border-primary text-primary" : "border-transparent text-text-muted hover:text-text")}>
                {tab.label}
                {tab.key === "candidates" && initialCandidates.length ? <span className="ml-1.5 rounded-full bg-bg-hover px-1.5 py-0.5 font-mono text-[9px]">{initialCandidates.length}</span> : null}
                {tab.key === "activity" && initialEvents.length ? <span className="ml-1.5 rounded-full bg-bg-hover px-1.5 py-0.5 font-mono text-[9px]">{initialEvents.length}</span> : null}
              </button>
            ))}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
            {partialFailure ? <div className="mb-4 flex items-center gap-2 rounded-lg border border-score-mid/25 bg-score-mid/5 px-3 py-2 text-[10.5px] text-score-mid"><AlertCircle className="h-3.5 w-3.5" />{copy.common.unavailable}</div> : null}
            {pendingBundles.length ? (
              <Link href="/ai/approvals" className="mb-4 flex items-center gap-3 rounded-xl border border-score-mid/30 bg-score-mid/5 p-3 text-score-mid transition-colors hover:bg-score-mid/10">
                <ShieldAlert className="h-4 w-4" />
                <span className="flex-1 text-[11.5px] font-medium">{pendingBundles.length} {copy.common.pending}</span>
                <ChevronRight className="h-4 w-4" />
              </Link>
            ) : null}

            {!mission ? (
              <div className="flex min-h-[360px] flex-col items-center justify-center text-center"><Loader2 className="h-6 w-6 animate-spin text-primary" /><h2 className="mt-4 text-[14px] font-semibold">{copy.mission.thinking}</h2><p className="mt-1 max-w-sm text-[11.5px] leading-5 text-text-muted">{copy.empty.messages}</p></div>
            ) : canvasTab === "overview" ? (
              <BriefCanvas mission={mission} copy={copy} locale={locale} onReplan={() => runControl("replan")} replanPending={controlPending} />
            ) : canvasTab === "plan" ? (
              <PlanCanvas mission={mission} copy={copy} locale={locale} />
            ) : canvasTab === "candidates" ? (
              <CandidatesCanvas candidates={orderedCandidates} expanded={expandedCandidate} onExpand={setExpandedCandidate} copy={copy} locale={locale} />
            ) : canvasTab === "evidence" ? (
              <EvidenceCanvas candidates={orderedCandidates} copy={copy} />
            ) : (
              <ActivityCanvas events={initialEvents} copy={copy} locale={locale} />
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: AiMessage }) {
  const assistant = message.role === "assistant";
  return (
    <div className={cn("flex gap-2.5", assistant ? "items-start" : "items-start justify-end")}>
      {assistant ? <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-accent-muted text-primary"><Bot className="h-3.5 w-3.5" /></span> : null}
      <div className={cn("max-w-[85%] whitespace-pre-wrap rounded-xl px-3 py-2.5 text-[12px] leading-5", assistant ? "border border-border bg-background text-text" : "bg-primary text-primary-foreground")}>
        {message.content}
        {message.status === "streaming" ? <span className="ml-1 inline-block h-3 w-1 animate-pulse rounded-full bg-current align-middle" /> : null}
      </div>
      {!assistant ? <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-bg-hover text-text-muted"><User className="h-3.5 w-3.5" /></span> : null}
    </div>
  );
}

function SectionCard({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return <section className={cn("rounded-xl border border-border bg-bg-elevated p-4", className)}><h3 className="mb-3 text-[11px] font-semibold uppercase tracking-[0.06em] text-text-faint">{title}</h3>{children}</section>;
}

function BriefCanvas({ mission, copy, locale, onReplan, replanPending }: { mission: HiringMission; copy: ReturnType<typeof getAiCopy>; locale: "en" | "ru"; onReplan: () => void; replanPending: boolean }) {
  const brief = mission.brief;
  if (!brief) return <CanvasEmpty icon={<Sparkles />} text={copy.mission.thinking} />;
  const compensation = brief.compensation ? `${brief.compensation.min.toLocaleString()}–${brief.compensation.max.toLocaleString()} ${brief.compensation.currency}` : copy.mission.noValue;
  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div className="flex flex-col gap-3 rounded-xl border border-accent-border bg-accent-muted p-4 sm:flex-row sm:items-start sm:justify-between">
        <div><div className="text-[10px] font-medium uppercase tracking-[0.08em] text-primary">{copy.mission.overview}</div><h2 className="mt-1 text-[20px] font-semibold tracking-tight">{brief.role_title}</h2></div>
        <Button variant="outline" size="sm" disabled={replanPending} onClick={onReplan}><RotateCcw className={replanPending ? "animate-spin" : ""} />{copy.mission.replan}</Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {[
          [copy.mission.headcount, String(brief.headcount)],
          [copy.mission.shortlist, String(brief.shortlist_size)],
          [copy.mission.location, brief.location ?? copy.mission.noValue],
          [copy.mission.deadline, brief.deadline ? new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", { dateStyle: "medium" }).format(new Date(brief.deadline)) : copy.mission.noValue],
          [copy.mission.compensation, compensation],
        ].map(([label, value]) => <div key={label} className="rounded-xl border border-border bg-bg-elevated p-3"><div className="text-[9px] uppercase tracking-[0.06em] text-text-faint">{label}</div><div className="mt-1 text-[12px] font-medium">{value}</div></div>)}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <SectionCard title={copy.mission.mustHaves}><BulletList items={brief.must_haves} empty={copy.mission.noValue} tone="check" /></SectionCard>
        <SectionCard title={copy.mission.preferences}><BulletList items={brief.preferences} empty={copy.mission.noValue} /></SectionCard>
        <SectionCard title={copy.mission.responsibilities}><BulletList items={brief.responsibilities} empty={copy.mission.noValue} /></SectionCard>
        <SectionCard title={copy.mission.rubric}>{mission.rubric?.criteria.length ? <div className="space-y-2">{mission.rubric.criteria.map((criterion) => <div key={criterion.key} className="flex items-center gap-3"><div className="h-1.5 flex-1 overflow-hidden rounded-full bg-bg-hover"><div className="h-full rounded-full bg-primary" style={{ width: `${criterion.weight}%` }} /></div><span className="min-w-0 flex-1 truncate text-[11px]">{criterion.key}</span><span className="font-mono text-[10px] text-text-faint">{criterion.weight}%</span></div>)}</div> : <span className="text-[11px] text-text-muted">{copy.mission.noValue}</span>}</SectionCard>
      </div>
      {brief.assumptions.length || brief.blockers.length ? <div className="grid gap-4 lg:grid-cols-2">{brief.assumptions.length ? <SectionCard title={copy.mission.assumptions}><BulletList items={brief.assumptions} empty={copy.mission.noValue} /></SectionCard> : null}{brief.blockers.length ? <SectionCard title={copy.mission.blockers}><BulletList items={brief.blockers} empty={copy.mission.noValue} tone="alert" /></SectionCard> : null}</div> : null}
    </div>
  );
}

function PlanCanvas({ mission, copy, locale }: { mission: HiringMission; copy: ReturnType<typeof getAiCopy>; locale: "en" | "ru" }) {
  const steps = mission.plan?.steps ?? mission.steps ?? [];
  if (!steps.length) return <CanvasEmpty icon={<Loader2 className="animate-spin" />} text={copy.mission.thinking} />;
  return <div className="mx-auto max-w-3xl"><div className="mb-4 flex items-center justify-between"><div><h2 className="text-[15px] font-semibold">{copy.mission.plan}</h2><p className="mt-1 text-[10.5px] text-text-faint">{copy.mission.planVersion} {mission.current_plan_version}</p></div></div><div className="relative ml-3 border-l border-border pl-7">{steps.map((step, index) => <div key={step.id} className="relative pb-6 last:pb-0"><span className={cn("absolute -left-[39px] top-0 flex h-6 w-6 items-center justify-center rounded-full border bg-background font-mono text-[9px]", step.status === "completed" ? "border-score-high/40 text-score-high" : step.status === "running" ? "border-accent-border text-primary" : "border-border text-text-faint")}>{step.status === "completed" ? <Check className="h-3 w-3" /> : index + 1}</span><div className="rounded-xl border border-border bg-bg-elevated p-4"><div className="flex flex-wrap items-start justify-between gap-2"><div><h3 className="text-[12.5px] font-medium">{localizedStepTitle(step.key ?? step.step_type, step.title, locale)}</h3>{step.description ? <p className="mt-1 text-[11px] leading-5 text-text-muted">{step.description}</p> : null}</div><StepStatus status={step.status} locale={locale} /></div></div></div>)}</div></div>;
}

function CandidatesCanvas({ candidates, expanded, onExpand, copy, locale }: { candidates: MissionCandidateAssessment[]; expanded: string | null; onExpand: (id: string | null) => void; copy: ReturnType<typeof getAiCopy>; locale: "en" | "ru" }) {
  if (!candidates.length) return <CanvasEmpty icon={<Target />} text={copy.empty.candidates} />;
  return <div className="mx-auto max-w-5xl space-y-2">{candidates.map((assessment, index) => { const open = expanded === assessment.candidate_id; const name = assessment.candidate?.display_name ?? assessment.candidate?.name ?? `${locale === "ru" ? "Кандидат" : "Candidate"} ${index + 1}`; const evidenceCoveragePercent = Math.max(0, Math.min(100, assessment.evidence_coverage * 100)); return <div key={assessment.id} className={cn("overflow-hidden rounded-xl border bg-bg-elevated", open ? "border-accent-border" : "border-border")}><button type="button" onClick={() => onExpand(open ? null : assessment.candidate_id)} className="flex w-full items-center gap-3 p-3 text-left sm:p-4"><span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-bg-hover font-mono text-[10px] text-text-muted">{index + 1}</span><div className="min-w-0 flex-1"><div className="truncate text-[12.5px] font-medium">{name}</div><div className="mt-0.5 truncate text-[10px] text-text-faint">{assessment.candidate?.current_title ?? assessment.candidate?.source ?? copy.mission.noValue}</div></div><RecommendationPill recommendation={assessment.recommendation} locale={locale} /><div className="w-14 text-right"><div className="font-mono text-[17px] font-semibold">{Math.round(assessment.overall_score)}</div><div className="text-[8px] uppercase text-text-faint">score</div></div><ChevronDown className={cn("h-4 w-4 text-text-faint transition-transform", open && "rotate-180")} /></button>{open ? <div className="border-t border-border px-4 py-4"><p className="text-[11.5px] leading-5 text-text-muted">{assessment.concise_rationale}</p><div className="mt-4 grid gap-3 sm:grid-cols-2"><SectionCard title={copy.mission.evidenceCoverage}><div className="flex items-center gap-3"><div className="h-2 flex-1 overflow-hidden rounded-full bg-bg-hover"><div className="h-full rounded-full bg-score-high" style={{ width: `${evidenceCoveragePercent}%` }} /></div><span className="font-mono text-[11px]">{Math.round(evidenceCoveragePercent)}%</span></div></SectionCard><SectionCard title={copy.mission.missingEvidence}><BulletList items={assessment.missing_evidence} empty="—" /></SectionCard></div><div className="mt-3 space-y-2">{assessment.criteria.map((criterion) => <div key={criterion.key} className="grid grid-cols-[1fr_60px] items-center gap-3 rounded-lg bg-bg-hover px-3 py-2"><span className="truncate text-[10.5px]">{criterion.key}</span><span className="text-right font-mono text-[11px]">{Math.round(criterion.score)}</span></div>)}</div><div className="mt-4 flex justify-end"><Button render={<Link href={`/candidates/${assessment.candidate_id}`} />} variant="outline" size="sm"><ExternalLink />{copy.mission.viewCandidate}</Button></div></div> : null}</div>; })}</div>;
}

function RecommendationPill({ recommendation, locale }: { recommendation: MissionCandidateAssessment["recommendation"]; locale: "en" | "ru" }) {
  const labels = { en: { strong_fit: "Strong fit", fit: "Fit", possible_fit: "Possible", insufficient_evidence: "More evidence", not_recommended: "Not recommended" }, ru: { strong_fit: "Сильный", fit: "Подходит", possible_fit: "Возможно", insufficient_evidence: "Мало данных", not_recommended: "Не рекомендован" } } as const;
  const tone = recommendation === "strong_fit" || recommendation === "fit" ? "bg-score-high/10 text-score-high" : recommendation === "not_recommended" ? "bg-score-low/10 text-score-low" : "bg-score-mid/10 text-score-mid";
  return <span className={cn("hidden rounded-full px-2 py-1 text-[9.5px] font-medium sm:inline", tone)}>{labels[locale][recommendation]}</span>;
}

function EvidenceCanvas({ candidates, copy }: { candidates: MissionCandidateAssessment[]; copy: ReturnType<typeof getAiCopy> }) {
  const claims = candidates.flatMap((candidate) => candidate.evidence.map((claim) => ({ ...claim, candidate: candidate.candidate })));
  if (!claims.length) return <CanvasEmpty icon={<FileSearch />} text={copy.empty.candidates} />;
  return <div className="mx-auto max-w-4xl space-y-3">{claims.map((claim: EvidenceClaim & { candidate?: MissionCandidateAssessment["candidate"] }) => <div key={claim.id} className="rounded-xl border border-border bg-bg-elevated p-4"><div className="flex items-center gap-2 text-[10px] text-text-faint"><FileSearch className="h-3.5 w-3.5 text-primary" /><span>{claim.candidate?.display_name ?? claim.candidate?.name ?? claim.candidate_id}</span><span>·</span><span>{claim.source_label ?? claim.source_type}</span></div><p className="mt-2 text-[12px] leading-5">{claim.claim}</p>{claim.excerpt ? <blockquote className="mt-2 border-l-2 border-accent-border pl-3 text-[10.5px] leading-5 text-text-muted">{claim.excerpt}</blockquote> : null}</div>)}</div>;
}

function ActivityCanvas({ events, copy, locale }: { events: MissionEvent[]; copy: ReturnType<typeof getAiCopy>; locale: "en" | "ru" }) {
  if (!events.length) return <CanvasEmpty icon={<MessageSquare />} text={copy.empty.activity} />;
  return <div className="mx-auto max-w-3xl"><div className="relative ml-3 border-l border-border pl-7">{events.map((event) => <div key={event.id} className="relative pb-5"><span className="absolute -left-[36px] top-0 flex h-4 w-4 items-center justify-center rounded-full border border-border bg-background"><span className={cn("h-1.5 w-1.5 rounded-full", event.actor_kind === "ai" ? "bg-primary" : "bg-text-faint")} /></span><div className="flex flex-wrap items-baseline gap-x-2"><span className="text-[11.5px] font-medium">{localizedEventTitle(event.event_type, locale)}</span><span className="text-[9.5px] text-text-faint">{event.actor_kind === "ai" ? copy.aiProduct : event.actor_kind}</span></div><time className="mt-1 block text-[9.5px] text-text-faint">{new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", { dateStyle: "medium", timeStyle: "short" }).format(new Date(event.occurred_at))}</time></div>)}</div></div>;
}

function localizedStepTitle(key: string, fallback: string | undefined, locale: "en" | "ru") {
  const labels = {
    en: { plan_request: "Build hiring brief and rubric", discover: "Discover candidates", assess: "Evaluate evidence", shortlist: "Prepare shortlist" },
    ru: { plan_request: "Подготовить профиль роли и критерии", discover: "Найти кандидатов", assess: "Оценить доказательства", shortlist: "Собрать шорт-лист" },
  } as const;
  return labels[locale][key as keyof typeof labels.en] ?? fallback ?? key.replaceAll("_", " ");
}

function localizedEventTitle(eventType: string, locale: "en" | "ru") {
  const labels = {
    en: { mission_planned: "Mission plan created", mission_replanned: "Mission plan updated", mission_stopped: "Mission stopped", mission_retried: "Mission retried", mission_failed: "Mission needs attention", brief_generated: "Hiring brief generated", candidate_discovery_completed: "Candidate discovery completed", candidate_assessments_completed: "Candidate assessments completed", shortlist_prepared: "Shortlist prepared", action_bundle_rejected: "Proposed action rejected", action_bundle_failed: "Approved action failed", action_bundle_executed: "Approved action executed" },
    ru: { mission_planned: "План миссии создан", mission_replanned: "План миссии обновлён", mission_stopped: "Миссия остановлена", mission_retried: "Миссия перезапущена", mission_failed: "Миссии нужна помощь", brief_generated: "Профиль роли подготовлен", candidate_discovery_completed: "Поиск кандидатов завершён", candidate_assessments_completed: "Оценка кандидатов завершена", shortlist_prepared: "Шорт-лист готов", action_bundle_rejected: "Предложенное действие отклонено", action_bundle_failed: "Согласованное действие не выполнено", action_bundle_executed: "Согласованное действие выполнено" },
  } as const;
  return labels[locale][eventType as keyof typeof labels.en] ?? eventType.replaceAll("_", " ");
}

function CanvasEmpty({ icon, text }: { icon: ReactNode; text: string }) { return <div className="flex min-h-[360px] flex-col items-center justify-center px-6 text-center"><span className="flex h-11 w-11 items-center justify-center rounded-xl border border-border bg-bg-elevated text-text-muted [&>svg]:h-5 [&>svg]:w-5">{icon}</span><p className="mt-4 max-w-sm text-[12px] leading-5 text-text-muted">{text}</p></div>; }

function BulletList({ items, empty, tone = "default" }: { items: string[]; empty: string; tone?: "default" | "check" | "alert" }) { if (!items.length) return <span className="text-[11px] text-text-muted">{empty}</span>; return <ul className="space-y-2">{items.map((item, index) => <li key={`${item}-${index}`} className="flex gap-2 text-[11px] leading-5 text-text-muted"><span className={cn("mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full", tone === "check" ? "bg-score-high" : tone === "alert" ? "bg-score-mid" : "bg-primary")} />{item}</li>)}</ul>; }
