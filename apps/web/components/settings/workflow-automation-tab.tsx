"use client";

import Link from "next/link";
import {
  Activity,
  CheckCircle2,
  ChevronDown,
  CircleStop,
  Eye,
  Loader2,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Trash2,
  Workflow,
  X,
  XCircle,
} from "lucide-react";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  createWorkflowRuleAction,
  cancelWorkflowRunAction,
  listWorkflowRulesAction,
  listWorkflowRunsAction,
  pauseWorkflowRuleAction,
  previewWorkflowRuleAction,
  processWorkflowsAction,
  replayWorkflowRunAction,
  resumeWorkflowRuleAction,
  updateWorkflowRuleAction,
} from "@/lib/actions";
import {
  KANBAN_STAGES,
  type ApplicationDisposition,
  type AppUser,
  type CommunicationTemplate,
  type KanbanStage,
  type MessageChannel,
  type Position,
  type WorkflowAction,
  type WorkflowConditions,
  type WorkflowDryRun,
  type WorkflowRule,
  type WorkflowRun,
  type WorkflowRunPage,
  type WorkflowTrigger,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/** @ui-only Workflow workspace section state. */
type Section = "rules" | "history";
/** @ui-only Workflow-run filter projection. */
type RunStatus = WorkflowRun["status"];
/** @ui-only Workflow action editor discriminator. */
type ActionType = WorkflowAction["type"];
/** @ui-only Workflow action with a stable client editor key. */
type DraftAction = WorkflowAction & { clientId: string };

const TRIGGERS: WorkflowTrigger[] = [
  "candidate_added",
  "candidate_scored",
  "candidate_rescored",
  "stage_changed",
  "scorecard_submitted",
  "message_received",
  "interview_scheduled",
  "candidate_inactive",
  "feedback_overdue",
];
const ACTION_TYPES: ActionType[] = [
  "move_stage",
  "assign_owner",
  "set_due_date",
  "add_tag",
  "create_task",
  "send_template",
  "notify_owner",
];
const RUN_STATUSES: RunStatus[] = [
  "queued",
  "running",
  "retrying",
  "completed",
  "skipped",
  "failed",
  "dead_letter",
  "cancelled",
];
const CHANNELS: MessageChannel[] = ["email", "telegram"];
const DISPOSITIONS: ApplicationDisposition[] = [
  "not_qualified",
  "withdrew",
  "compensation",
  "timing",
  "duplicate",
  "position_closed",
  "other",
];
const controlClass =
  "min-h-9 w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[12px] text-text outline-none transition-colors focus:border-border-strong";

function draftAction(type: ActionType, seed: number): DraftAction {
  const clientId = `action-${seed}`;
  switch (type) {
    case "move_stage":
      return { clientId, type, stage: "screening" };
    case "assign_owner":
      return { clientId, type, user_id: "" };
    case "set_due_date":
      return { clientId, type, days_from_now: 1 };
    case "add_tag":
      return { clientId, type, label: "" };
    case "create_task":
      return { clientId, type, title: "", prompt: null };
    case "send_template":
      return { clientId, type, channel: "email", template_id: "" };
    case "notify_owner":
      return { clientId, type, title: "" };
  }
}

function withClientId(action: WorkflowAction, index: number): DraftAction {
  return { ...action, clientId: `action-${index}-${Date.now()}` } as DraftAction;
}

function isActionValid(action: DraftAction): boolean {
  switch (action.type) {
    case "move_stage":
      return Boolean(action.stage) && (
        action.stage !== "rejected" ||
        Boolean(action.disposition_code && action.reason?.trim())
      );
    case "assign_owner":
      return Boolean(action.user_id);
    case "set_due_date":
      return Number.isInteger(action.days_from_now) && action.days_from_now >= 0 && action.days_from_now <= 365;
    case "add_tag":
      return Boolean(action.label.trim()) && action.label.trim().length <= 32;
    case "create_task":
      return Boolean(action.title.trim());
    case "send_template":
      return Boolean(action.template_id);
    case "notify_owner":
      return Boolean(action.title.trim()) && action.title.trim().length <= 160;
  }
}

export function WorkflowAutomationTab({
  initialRules,
  initialRuns,
  positions,
  users,
  communicationTemplates,
  rulesLoadFailed,
  runsLoadFailed,
  canManage,
  canExecute,
}: {
  initialRules: WorkflowRule[];
  initialRuns: WorkflowRunPage;
  positions: Position[];
  users: AppUser[];
  communicationTemplates: CommunicationTemplate[];
  rulesLoadFailed: boolean;
  runsLoadFailed: boolean;
  canManage: boolean;
  canExecute: boolean;
}) {
  const { m } = useI18n();
  const copy = m.workflows;
  const [section, setSection] = useState<Section>("rules");
  const [rules, setRules] = useState(initialRules);
  const [runs, setRuns] = useState(initialRuns.items);
  const [nextCursor, setNextCursor] = useState(initialRuns.next_cursor);
  const [rulesError, setRulesError] = useState(rulesLoadFailed);
  const [runsError, setRunsError] = useState(runsLoadFailed);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [trigger, setTrigger] = useState<WorkflowTrigger>("candidate_added");
  const [positionId, setPositionId] = useState("");
  const [minScore, setMinScore] = useState("");
  const [maxScore, setMaxScore] = useState("");
  const [stages, setStages] = useState<KanbanStage[]>([]);
  const [sources, setSources] = useState("");
  const [inactivityDays, setInactivityDays] = useState("7");
  const [actions, setActions] = useState<DraftAction[]>([
    draftAction("notify_owner", 1),
  ]);
  const [active, setActive] = useState(true);
  const [runRuleFilter, setRunRuleFilter] = useState("");
  const [runStatusFilter, setRunStatusFilter] = useState<RunStatus | "">("");
  const [runningRuleId, setRunningRuleId] = useState<string | null>(null);
  const [previewingRuleId, setPreviewingRuleId] = useState<string | null>(null);
  const [replayingRunId, setReplayingRunId] = useState<string | null>(null);
  const [cancellingRunId, setCancellingRunId] = useState<string | null>(null);
  const [rulePreviews, setRulePreviews] = useState<Record<string, WorkflowDryRun>>({});
  const [rulePending, startRuleTransition] = useTransition();
  const [runPending, startRunTransition] = useTransition();

  const parsedSources = sources
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  const minScoreNumber = minScore === "" ? null : Number(minScore);
  const maxScoreNumber = maxScore === "" ? null : Number(maxScore);

  const valid =
    name.trim().length > 0 &&
    actions.length > 0 &&
    actions.length <= 10 &&
    actions.every(isActionValid) &&
    parsedSources.length <= 20 &&
    parsedSources.every((source) => source.length <= 64) &&
    (minScoreNumber == null ||
      (Number.isInteger(minScoreNumber) && minScoreNumber >= 0 && minScoreNumber <= 100)) &&
    (maxScoreNumber == null ||
      (Number.isInteger(maxScoreNumber) && maxScoreNumber >= 0 && maxScoreNumber <= 100)) &&
    (minScoreNumber == null ||
      maxScoreNumber == null ||
      minScoreNumber <= maxScoreNumber) &&
    (trigger !== "candidate_inactive" ||
      (Number.isInteger(Number(inactivityDays)) &&
        Number(inactivityDays) >= 1 &&
        Number(inactivityDays) <= 365));

  function resetForm() {
    setEditingId(null);
    setName("");
    setTrigger("candidate_added");
    setPositionId("");
    setMinScore("");
    setMaxScore("");
    setStages([]);
    setSources("");
    setInactivityDays("7");
    setActions([draftAction("notify_owner", Date.now())]);
    setActive(true);
    setFormOpen(false);
  }

  function editRule(rule: WorkflowRule) {
    if (!canManage) return;
    setEditingId(rule.id);
    setName(rule.name);
    setTrigger(rule.trigger);
    setPositionId(rule.conditions.position_id ?? "");
    setMinScore(rule.conditions.min_score == null ? "" : String(rule.conditions.min_score));
    setMaxScore(rule.conditions.max_score == null ? "" : String(rule.conditions.max_score));
    setStages(rule.conditions.stage_in ?? []);
    setSources((rule.conditions.source_in ?? []).join(", "));
    setInactivityDays(String(rule.conditions.inactivity_days ?? 7));
    setActions(rule.actions.map(withClientId));
    setActive(rule.is_active);
    setFormOpen(true);
    setSection("rules");
  }

  function conditionsPayload(): WorkflowConditions {
    return {
      position_id: positionId || null,
      min_score: minScore === "" ? null : Number(minScore),
      max_score: maxScore === "" ? null : Number(maxScore),
      stage_in: stages.length ? stages : null,
      source_in: parsedSources.length ? parsedSources : null,
      inactivity_days:
        trigger === "candidate_inactive" ? Number(inactivityDays) : null,
    };
  }

  function actionPayload(): WorkflowAction[] {
    return actions.map(({ clientId: _clientId, ...action }) => action as WorkflowAction);
  }

  function saveRule(event: React.FormEvent) {
    event.preventDefault();
    if (!canManage || !valid || rulePending) return;
    startRuleTransition(async () => {
      const result = editingId
        ? await updateWorkflowRuleAction(editingId, {
            name: name.trim(),
            conditions: conditionsPayload(),
            actions: actionPayload(),
            is_active: active,
          })
        : await createWorkflowRuleAction({
            name: name.trim(),
            trigger,
            conditions: conditionsPayload(),
            actions: actionPayload(),
            is_active: active,
          });
      if (!result.ok) {
        toast.error(copy.ruleSaveFailed);
        return;
      }
      setRules((current) => {
        const exists = current.some((rule) => rule.id === result.data.id);
        return exists
          ? current.map((rule) => (rule.id === result.data.id ? result.data : rule))
          : [result.data, ...current];
      });
      toast.success(editingId ? copy.ruleUpdated : copy.ruleCreated);
      resetForm();
    });
  }

  function toggleRule(rule: WorkflowRule) {
    if (!canManage) return;
    startRuleTransition(async () => {
      if (rule.is_active) {
        const result = await pauseWorkflowRuleAction(rule.id);
        if (!result.ok) {
          toast.error(copy.ruleSaveFailed);
          return;
        }
        setRules((current) =>
          current.map((item) =>
            item.id === rule.id ? result.data : item,
          ),
        );
      } else {
        const result = await resumeWorkflowRuleAction(rule.id);
        if (!result.ok) {
          toast.error(copy.ruleSaveFailed);
          return;
        }
        setRules((current) =>
          current.map((item) =>
            item.id === rule.id ? result.data : item,
          ),
        );
      }
      toast.success(copy.ruleUpdated);
    });
  }

  function previewRule(ruleId: string) {
    if (!canManage || previewingRuleId !== null) return;
    setPreviewingRuleId(ruleId);
    startRunTransition(async () => {
      try {
        const result = await previewWorkflowRuleAction(ruleId, { limit: 20 });
        if (!result.ok) {
          toast.error(copy.previewFailed);
          return;
        }
        setRulePreviews((current) => ({ ...current, [ruleId]: result.data }));
        toast.success(copy.previewResult(result.data.matched, result.data.evaluated));
      } finally {
        setPreviewingRuleId(null);
      }
    });
  }

  function replayRun(runId: string) {
    if (!canExecute || replayingRunId !== null) return;
    setReplayingRunId(runId);
    startRunTransition(async () => {
      try {
        const result = await replayWorkflowRunAction(runId);
        if (!result.ok) {
          toast.error(copy.replayFailed);
          return;
        }
        setRuns((current) => {
          const withoutReplay = current.filter((run) => run.id !== result.data.id);
          return [result.data, ...withoutReplay];
        });
        toast.success(copy.replayQueued);
      } finally {
        setReplayingRunId(null);
      }
    });
  }

  function cancelRun(runId: string) {
    if (!canExecute || cancellingRunId !== null) return;
    setCancellingRunId(runId);
    startRunTransition(async () => {
      try {
        const result = await cancelWorkflowRunAction(runId);
        if (!result.ok) {
          toast.error(result.error || copy.cancelFailed);
          return;
        }
        setRuns((current) =>
          current.map((run) => (run.id === result.data.id ? result.data : run)),
        );
        toast.success(
          result.data.status === "cancelled"
            ? copy.cancelled
            : copy.cancellationRequested,
        );
      } finally {
        setCancellingRunId(null);
      }
    });
  }

  function retryRules() {
    startRuleTransition(async () => {
      const result = await listWorkflowRulesAction({ includeInactive: true });
      if (!result.ok) {
        setRulesError(true);
        return;
      }
      setRules(result.data);
      setRulesError(false);
    });
  }

  function replaceAction(clientId: string, action: WorkflowAction) {
    setActions((current) =>
      current.map((item) =>
        item.clientId === clientId ? ({ ...action, clientId } as DraftAction) : item,
      ),
    );
  }

  function changeActionType(clientId: string, type: ActionType) {
    replaceAction(clientId, draftAction(type, Date.now()));
  }

  function toggleStage(stage: KanbanStage) {
    setStages((current) =>
      current.includes(stage)
        ? current.filter((item) => item !== stage)
        : [...current, stage],
    );
  }

  function refreshRuns(ruleId = runRuleFilter, status = runStatusFilter) {
    startRunTransition(async () => {
      const result = await listWorkflowRunsAction({
        ruleId: ruleId || null,
        status: status || null,
      });
      if (!result.ok) {
        setRunsError(true);
        return;
      }
      setRuns(result.data.items);
      setNextCursor(result.data.next_cursor);
      setRunsError(false);
    });
  }

  function loadMoreRuns() {
    if (!nextCursor) return;
    startRunTransition(async () => {
      const result = await listWorkflowRunsAction({
        cursor: nextCursor,
        ruleId: runRuleFilter || null,
        status: runStatusFilter || null,
      });
      if (!result.ok) {
        setRunsError(true);
        return;
      }
      setRuns((current) => {
        const seen = new Set(current.map((run) => run.id));
        return [...current, ...result.data.items.filter((run) => !seen.has(run.id))];
      });
      setNextCursor(result.data.next_cursor);
      setRunsError(false);
    });
  }

  function runNow(ruleId: string | null) {
    if (!canExecute || runningRuleId !== null) return;
    setRunningRuleId(ruleId ?? "all");
    startRunTransition(async () => {
      try {
        const result = await processWorkflowsAction(ruleId);
        if (!result.ok) {
          toast.error(copy.ruleSaveFailed);
          return;
        }
        toast.success(copy.processResult(result.data.queued));
        refreshRuns();
      } finally {
        setRunningRuleId(null);
      }
    });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-[16px] font-semibold text-text">{copy.title}</h2>
          <p className="mt-1 text-[12px] text-text-muted">{copy.subtitle}</p>
        </div>
        <div className="flex gap-2">
          {canExecute ? <Button size="sm" variant="outline" onClick={() => runNow(null)} disabled={runPending || runningRuleId !== null}>
            {runningRuleId === "all" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            {runningRuleId === "all" ? copy.running : copy.runNow}
          </Button> : null}
          {canManage ? <Button size="sm" variant={formOpen ? "ghost" : "default"} onClick={() => (formOpen ? resetForm() : setFormOpen(true))}>
            {formOpen ? <X className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
            {formOpen ? m.common.cancel : copy.newRule}
          </Button> : null}
        </div>
      </div>

      <div className="flex gap-1 border-b border-border">
        <SubTab active={section === "rules"} onClick={() => setSection("rules")}>{copy.rules}</SubTab>
        <SubTab active={section === "history"} onClick={() => setSection("history")}>{copy.history}</SubTab>
      </div>

      {section === "rules" ? (
        <>
          {rulesError ? (
            <FailureNotice copy={copy.loadFailed} retry={copy.retry} pending={rulePending} onRetry={retryRules} />
          ) : null}

          {canManage && formOpen ? (
            <form onSubmit={saveRule} className="rounded-xl border border-border bg-bg-elevated p-5">
              <div className="mb-4 flex items-center gap-2">
                <Workflow className="h-4 w-4 text-primary" />
                <h3 className="text-[12px] font-semibold text-text">{editingId ? copy.editRule : copy.newRule}</h3>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <Field label={copy.name}>
                  <input value={name} onChange={(event) => setName(event.target.value)} maxLength={160} placeholder={copy.namePlaceholder} className={controlClass} />
                </Field>
                <Field label={copy.trigger}>
                  <select value={trigger} onChange={(event) => setTrigger(event.target.value as WorkflowTrigger)} disabled={Boolean(editingId)} className={controlClass}>
                    {TRIGGERS.map((value) => <option key={value} value={value}>{copy.triggers[value]}</option>)}
                  </select>
                </Field>
              </div>
              {editingId ? <p className="mt-2 text-[10.5px] text-text-faint">{copy.triggerImmutable}</p> : null}

              <h4 className="mb-3 mt-6 text-[11px] font-semibold uppercase tracking-[0.07em] text-text-muted">{copy.conditions}</h4>
              <div className="grid gap-3 md:grid-cols-3">
                <Field label={copy.position}>
                  <select value={positionId} onChange={(event) => setPositionId(event.target.value)} className={controlClass}>
                    <option value="">{copy.anyPosition}</option>
                    {positions.filter((position) => position.status !== "closed").map((position) => <option key={position.id} value={position.id}>{position.title}</option>)}
                  </select>
                </Field>
                <Field label={copy.minScore}>
                  <input type="number" min={0} max={100} value={minScore} onChange={(event) => setMinScore(event.target.value)} className={controlClass} />
                </Field>
                <Field label={copy.maxScore}>
                  <input type="number" min={0} max={100} value={maxScore} onChange={(event) => setMaxScore(event.target.value)} className={controlClass} />
                </Field>
                <Field label={copy.sources}>
                  <input value={sources} onChange={(event) => setSources(event.target.value)} maxLength={500} placeholder={copy.sourcesPlaceholder} className={controlClass} />
                </Field>
                {trigger === "candidate_inactive" ? (
                  <Field label={copy.inactivityDays}>
                    <input type="number" min={1} max={365} value={inactivityDays} onChange={(event) => setInactivityDays(event.target.value)} className={controlClass} />
                  </Field>
                ) : null}
              </div>
              <fieldset className="mt-4">
                <legend className="text-[10.5px] font-medium text-text-muted">{copy.stages}</legend>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {KANBAN_STAGES.map((stage) => (
                    <label key={stage} className={cn("cursor-pointer rounded-[5px] border px-2.5 py-1.5 text-[10.5px]", stages.includes(stage) ? "border-accent-border bg-accent-muted text-text" : "border-border text-text-muted")}>
                      <input type="checkbox" checked={stages.includes(stage)} onChange={() => toggleStage(stage)} className="sr-only" />
                      {m.kanban.stages[stage]}
                    </label>
                  ))}
                </div>
              </fieldset>

              <div className="mb-3 mt-6 flex items-center justify-between gap-3">
                <h4 className="text-[11px] font-semibold uppercase tracking-[0.07em] text-text-muted">{copy.actions}</h4>
                <Button type="button" size="sm" variant="ghost" disabled={actions.length >= 10} onClick={() => setActions((current) => [...current, draftAction("notify_owner", Date.now())])}>
                  <Plus className="h-3.5 w-3.5" /> {copy.addAction}
                </Button>
              </div>
              <div className="space-y-3">
                {actions.map((action) => (
                  <ActionEditor key={action.clientId} action={action} users={users} templates={communicationTemplates} onChange={(next) => replaceAction(action.clientId, next)} onTypeChange={(type) => changeActionType(action.clientId, type)} onRemove={() => setActions((current) => current.filter((item) => item.clientId !== action.clientId))} />
                ))}
              </div>
              <label className="mt-4 flex items-center gap-2 text-[11px] text-text-muted">
                <input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} />
                {copy.active}
              </label>
              <div className="mt-4 flex justify-end">
                <Button type="submit" size="sm" disabled={!valid || rulePending}>
                  {rulePending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                  {rulePending ? copy.savingRule : editingId ? copy.saveRule : copy.createRule}
                </Button>
              </div>
            </form>
          ) : null}

          {rules.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-5 py-12 text-center text-[12px] text-text-muted">{copy.noRules}</div>
          ) : (
            <div className="space-y-3">
              {rules.map((rule) => (
                <RuleCard key={rule.id} rule={rule} preview={rulePreviews[rule.id] ?? null} pending={rulePending || runPending} running={runningRuleId === rule.id} previewing={previewingRuleId === rule.id} canManage={canManage} canExecute={canExecute} onEdit={() => editRule(rule)} onToggle={() => toggleRule(rule)} onRun={() => runNow(rule.id)} onPreview={() => previewRule(rule.id)} />
              ))}
            </div>
          )}
        </>
      ) : (
        <RunHistory
          runs={runs}
          rules={rules}
          ruleFilter={runRuleFilter}
          statusFilter={runStatusFilter}
          nextCursor={nextCursor}
          error={runsError}
          pending={runPending}
          onRuleFilter={(value) => {
            setRunRuleFilter(value);
            refreshRuns(value, runStatusFilter);
          }}
          onStatusFilter={(value) => {
            setRunStatusFilter(value);
            refreshRuns(runRuleFilter, value);
          }}
          onRetry={() => refreshRuns()}
          onLoadMore={loadMoreRuns}
          canReplay={canExecute}
          canCancel={canExecute}
          replayingRunId={replayingRunId}
          cancellingRunId={cancellingRunId}
          onReplay={replayRun}
          onCancel={cancelRun}
        />
      )}
    </div>
  );
}

function ActionEditor({
  action,
  users,
  templates,
  onChange,
  onTypeChange,
  onRemove,
}: {
  action: DraftAction;
  users: AppUser[];
  templates: CommunicationTemplate[];
  onChange: (action: WorkflowAction) => void;
  onTypeChange: (type: ActionType) => void;
  onRemove: () => void;
}) {
  const { m } = useI18n();
  const copy = m.workflows;
  return (
    <div className="rounded-[8px] border border-border bg-bg p-3.5">
      <div className="grid gap-3 md:grid-cols-[220px_minmax(0,1fr)_auto]">
        <Field label={copy.actions}>
          <select value={action.type} onChange={(event) => onTypeChange(event.target.value as ActionType)} className={controlClass}>
            {ACTION_TYPES.map((type) => <option key={type} value={type}>{copy.actionTypes[type]}</option>)}
          </select>
        </Field>
        <div className="min-w-0">
          {action.type === "move_stage" ? (
            <div className="grid gap-3 md:grid-cols-2">
              <Field label={copy.targetStage}>
                <select
                  value={action.stage}
                  onChange={(event) => {
                    const stage = event.target.value as KanbanStage;
                    onChange(
                      stage === "rejected"
                        ? {
                            type: "move_stage",
                            stage,
                            disposition_code:
                              action.disposition_code ?? "not_qualified",
                            reason: action.reason ?? "",
                          }
                        : { type: "move_stage", stage },
                    );
                  }}
                  className={controlClass}
                >
                  {KANBAN_STAGES.map((stage) => <option key={stage} value={stage}>{m.kanban.stages[stage]}</option>)}
                </select>
              </Field>
              {action.stage === "rejected" ? (
                <>
                  <Field label={m.kanban.dispositionLabel}>
                    <select
                      value={action.disposition_code ?? "not_qualified"}
                      onChange={(event) =>
                        onChange({
                          ...action,
                          disposition_code: event.target
                            .value as ApplicationDisposition,
                        })
                      }
                      className={controlClass}
                    >
                      {DISPOSITIONS.map((value) => (
                        <option key={value} value={value}>
                          {m.kanban.dispositions[value]}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <label className="grid gap-1.5 text-[10.5px] font-medium text-text-muted md:col-span-2">
                    {m.kanban.reasonLabel}
                    <textarea
                      value={action.reason ?? ""}
                      onChange={(event) =>
                        onChange({ ...action, reason: event.target.value })
                      }
                      maxLength={2000}
                      required
                      className={controlClass}
                    />
                  </label>
                </>
              ) : null}
            </div>
          ) : action.type === "assign_owner" ? (
            <Field label={copy.owner}>
              <select value={action.user_id} onChange={(event) => onChange({ type: "assign_owner", user_id: event.target.value })} className={controlClass}>
                <option value="">—</option>
                {users.map((user) => <option key={user.id} value={user.id}>{user.email}</option>)}
              </select>
            </Field>
          ) : action.type === "set_due_date" ? (
            <Field label={copy.daysFromNow}>
              <input type="number" min={0} max={365} value={action.days_from_now} onChange={(event) => onChange({ type: "set_due_date", days_from_now: Number(event.target.value) })} className={controlClass} />
            </Field>
          ) : action.type === "add_tag" ? (
            <Field label={copy.tag}>
              <input value={action.label} onChange={(event) => onChange({ type: "add_tag", label: event.target.value })} maxLength={32} className={controlClass} />
            </Field>
          ) : action.type === "create_task" ? (
            <div className="grid gap-3 md:grid-cols-2">
              <Field label={copy.taskTitle}>
                <input value={action.title} onChange={(event) => onChange({ ...action, title: event.target.value })} maxLength={200} className={controlClass} />
              </Field>
              <Field label={copy.taskPrompt}>
                <input value={action.prompt ?? ""} onChange={(event) => onChange({ ...action, prompt: event.target.value || null })} maxLength={2000} className={controlClass} />
              </Field>
            </div>
          ) : action.type === "send_template" ? (
            <div className="grid gap-3 md:grid-cols-2">
              <Field label={copy.channel}>
                <select value={action.channel} onChange={(event) => onChange({ type: "send_template", channel: event.target.value as MessageChannel, template_id: "" })} className={controlClass}>
                  {CHANNELS.map((channel) => <option key={channel} value={channel}>{channel}</option>)}
                </select>
              </Field>
              <Field label={copy.messageTemplate}>
                <select value={action.template_id} onChange={(event) => onChange({ ...action, template_id: event.target.value })} className={controlClass}>
                  <option value="">—</option>
                  {templates.filter((template) => template.is_active && template.channel === action.channel).map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}
                </select>
              </Field>
            </div>
          ) : (
            <Field label={copy.notificationTitle}>
              <input value={action.title} onChange={(event) => onChange({ type: "notify_owner", title: event.target.value })} maxLength={160} className={controlClass} />
            </Field>
          )}
        </div>
        <div className="flex items-end justify-end">
          <Button type="button" size="icon-sm" variant="ghost" title={copy.removeAction} onClick={onRemove}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
    </div>
  );
}

function RuleCard({
  rule,
  preview,
  pending,
  running,
  previewing,
  canManage,
  canExecute,
  onEdit,
  onToggle,
  onRun,
  onPreview,
}: {
  rule: WorkflowRule;
  preview: WorkflowDryRun | null;
  pending: boolean;
  running: boolean;
  previewing: boolean;
  canManage: boolean;
  canExecute: boolean;
  onEdit: () => void;
  onToggle: () => void;
  onRun: () => void;
  onPreview: () => void;
}) {
  const { m } = useI18n();
  const copy = m.workflows;
  return (
    <article className="rounded-xl border border-border bg-bg-elevated p-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[13px] font-semibold text-text">{rule.name}</h3>
            <span className={cn("rounded-[4px] px-1.5 py-0.5 text-[9.5px]", rule.is_active ? "bg-accent-muted text-primary" : "bg-bg-active text-text-faint")}>{rule.is_active ? copy.active : copy.inactive}</span>
          </div>
          <p className="mt-1 text-[11px] text-text-muted">{copy.triggers[rule.trigger] ?? rule.trigger}</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {rule.actions.map((action, index) => <span key={`${action.type}-${index}`} className="rounded-[4px] bg-bg px-2 py-1 text-[9.5px] text-text-faint">{copy.actionTypes[action.type]}</span>)}
          </div>
          <p className="mt-2 text-[10px] text-text-faint">{copy.ruleVersion(rule.version)}</p>
          <p className="mt-2 text-[10px] text-text-faint">{rule.last_processed_at ? copy.lastProcessed(m.format.relativeTime(rule.last_processed_at)) : copy.neverProcessed}</p>
        </div>
        <div className="flex shrink-0 gap-1.5">
          {canExecute ? <Button size="sm" variant="ghost" onClick={onRun} disabled={pending || !rule.is_active}>
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            {running ? copy.running : copy.runNow}
          </Button> : null}
          {canManage ? <Button size="sm" variant="ghost" onClick={onPreview} disabled={pending}>
            {previewing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Eye className="h-3.5 w-3.5" />}
            {previewing ? copy.previewing : copy.preview}
          </Button> : null}
          {canManage ? <Button size="icon-sm" variant="ghost" onClick={onEdit} disabled={pending} title={copy.editRule}><Pencil className="h-3.5 w-3.5" /></Button> : null}
          {canManage ? <Button size="sm" variant="ghost" onClick={onToggle} disabled={pending}>{rule.is_active ? copy.pause : copy.resume}</Button> : null}
        </div>
      </div>
      {preview ? (
        <div className="mt-4 rounded-lg border border-border bg-bg px-3.5 py-3" aria-live="polite">
          <p className="text-[10.5px] font-medium text-text">{copy.previewResult(preview.matched, preview.evaluated)}</p>
          <p className="mt-1 text-[10px] text-text-faint">{copy.previewNoEffects}</p>
          {preview.items.some((item) => item.matched) ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {preview.items.flatMap((item) => item.actions).map((action, index) => (
                <span key={`${action.index}-${index}`} className="rounded-[4px] bg-bg-active px-2 py-1 text-[9.5px] text-text-muted">
                  {copy.actionTypes[action.type] ?? action.type}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function RunHistory({
  runs,
  rules,
  ruleFilter,
  statusFilter,
  nextCursor,
  error,
  pending,
  onRuleFilter,
  onStatusFilter,
  onRetry,
  onLoadMore,
  canReplay,
  canCancel,
  replayingRunId,
  cancellingRunId,
  onReplay,
  onCancel,
}: {
  runs: WorkflowRun[];
  rules: WorkflowRule[];
  ruleFilter: string;
  statusFilter: RunStatus | "";
  nextCursor: string | null;
  error: boolean;
  pending: boolean;
  onRuleFilter: (value: string) => void;
  onStatusFilter: (value: RunStatus | "") => void;
  onRetry: () => void;
  onLoadMore: () => void;
  canReplay: boolean;
  canCancel: boolean;
  replayingRunId: string | null;
  cancellingRunId: string | null;
  onReplay: (runId: string) => void;
  onCancel: (runId: string) => void;
}) {
  const { m } = useI18n();
  const copy = m.workflows;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 rounded-xl border border-border bg-bg-elevated p-4 sm:grid-cols-2">
        <select value={ruleFilter} onChange={(event) => onRuleFilter(event.target.value)} className={controlClass}>
          <option value="">{copy.allRules}</option>
          {rules.map((rule) => <option key={rule.id} value={rule.id}>{rule.name}</option>)}
        </select>
        <select value={statusFilter} onChange={(event) => onStatusFilter(event.target.value as RunStatus | "")} className={controlClass}>
          <option value="">{copy.allStatuses}</option>
          {RUN_STATUSES.map((status) => <option key={status} value={status}>{copy.runStatuses[status]}</option>)}
        </select>
      </div>
      {error && runs.length === 0 ? <FailureNotice copy={copy.loadFailed} retry={copy.retry} pending={pending} onRetry={onRetry} /> : null}
      {pending && runs.length === 0 ? <div className="flex justify-center py-10"><Loader2 className="h-5 w-5 animate-spin text-text-faint" /></div> : runs.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border px-5 py-12 text-center text-[12px] text-text-muted">{copy.noRuns}</div>
      ) : (
        <div className="space-y-2">
          {runs.map((run) => <RunCard key={run.id} run={run} canReplay={canReplay} canCancel={canCancel} replaying={replayingRunId === run.id} cancelling={cancellingRunId === run.id} onReplay={() => onReplay(run.id)} onCancel={() => onCancel(run.id)} />)}
        </div>
      )}
      {nextCursor ? <div className="flex justify-center"><Button size="sm" variant="outline" onClick={onLoadMore} disabled={pending}>{pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ChevronDown className="h-3.5 w-3.5" />}{copy.loadMore}</Button></div> : null}
      {error && runs.length > 0 ? <p className="text-center text-[11px] text-destructive">{copy.loadMoreFailed}</p> : null}
    </div>
  );
}

function RunCard({
  run,
  canReplay,
  canCancel,
  replaying,
  cancelling,
  onReplay,
  onCancel,
}: {
  run: WorkflowRun;
  canReplay: boolean;
  canCancel: boolean;
  replaying: boolean;
  cancelling: boolean;
  onReplay: () => void;
  onCancel: () => void;
}) {
  const { m } = useI18n();
  const copy = m.workflows;
  const cancellationRequested =
    "cancellation_requested" in run && run.cancellation_requested === true;
  const attemptCount =
    "attempt_count" in run && typeof run.attempt_count === "number"
      ? run.attempt_count
      : null;
  const maxAttempts =
    "max_attempts" in run && typeof run.max_attempts === "number"
      ? run.max_attempts
      : null;
  const availableAt =
    "available_at" in run && typeof run.available_at === "string"
      ? run.available_at
      : null;
  const visibleError =
    run.error_code &&
    !["cancellation_requested", "cancelled_by_user"].includes(run.error_code)
      ? run.error_code
      : null;
  const isRetry = ["failed", "dead_letter"].includes(run.status);
  const statusIcon = run.status === "completed" ? <CheckCircle2 className="h-4 w-4 text-primary" /> : run.status === "failed" ? <XCircle className="h-4 w-4 text-destructive" /> : <Activity className="h-4 w-4 text-text-faint" />;
  return (
    <article className="rounded-xl border border-border bg-bg-elevated px-4 py-3.5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <span className="mt-0.5">{statusIcon}</span>
          <div className="min-w-0">
            <h3 className="truncate text-[12px] font-semibold text-text">{run.rule_name}</h3>
            <p className="mt-0.5 text-[10.5px] text-text-faint">{copy.triggers[run.trigger] ?? run.trigger} · {m.format.relativeTime(run.started_at)}</p>
          </div>
        </div>
        <span className="rounded-[4px] bg-bg px-2 py-1 text-[9.5px] text-text-muted">{copy.runStatuses[run.status] ?? run.status}</span>
      </div>
      {visibleError ? <p className="mt-2 rounded-[5px] bg-destructive/5 px-2.5 py-2 text-[10.5px] text-destructive">{copy.errorCodes[visibleError] ?? copy.errorCodes.internal_error}</p> : null}
      {cancellationRequested ? <p className="mt-2 rounded-[5px] bg-score-mid/5 px-2.5 py-2 text-[10.5px] text-score-mid" aria-live="polite">{copy.cancellationRequested}</p> : null}
      {run.action_results.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {run.action_results.map((result, index) => {
            const type = typeof result.type === "string" ? result.type : "";
            const status = typeof result.status === "string" ? result.status : "";
            const quotaUsage =
              type === "quota" &&
              typeof result.used === "number" &&
              typeof result.limit === "number"
                ? ` · ${copy.quotaUsage(result.used, result.limit)}`
                : "";
            return <span key={`${type}-${index}`} className="rounded-[4px] bg-bg px-2 py-1 text-[9.5px] text-text-faint">{copy.actionTypes[type] ?? copy.actions} · {copy.actionStatuses[status] ?? copy.runStatuses[run.status]}{quotaUsage}</span>;
          })}
        </div>
      ) : null}
      <div className="mt-2 flex items-center justify-between gap-2">
        <p className="text-[9.5px] text-text-faint">
          {copy.runVersion(run.rule_version)}
          {run.replay_of_run_id ? ` · ${copy.replayOf}` : ""}
          {attemptCount !== null && maxAttempts !== null ? ` · ${copy.runAttempts(attemptCount, maxAttempts)}` : ""}
          {run.status === "retrying" && availableAt ? ` · ${copy.retryAt(m.format.relativeTime(availableAt))}` : ""}
        </p>
        <div className="flex items-center gap-1.5">
          {canCancel && ["queued", "running", "retrying"].includes(run.status) ? (
            <Button size="sm" variant="ghost" onClick={onCancel} disabled={cancelling || cancellationRequested}>
              {cancelling ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CircleStop className="h-3.5 w-3.5" />}
              {cancelling ? copy.cancelling : cancellationRequested ? copy.cancellationRequestedShort : copy.cancelRun}
            </Button>
          ) : null}
          {canReplay && ["completed", "skipped", "failed", "dead_letter", "cancelled"].includes(run.status) ? (
            <Button size="sm" variant="ghost" onClick={onReplay} disabled={replaying}>
              {replaying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
              {replaying ? copy.replaying : isRetry ? copy.retryRun : copy.replay}
            </Button>
          ) : null}
          {run.candidate_id ? <Link href={`/candidates/${run.candidate_id}`} className="text-[10.5px] font-medium text-primary hover:underline">{copy.openCandidate}</Link> : null}
        </div>
      </div>
    </article>
  );
}

function FailureNotice({ copy, retry, pending, onRetry }: { copy: string; retry: string; pending: boolean; onRetry: () => void }) {
  return <div className="flex items-center justify-between gap-3 rounded-lg border border-score-mid/30 bg-score-mid/5 px-4 py-3 text-[11px] text-score-mid"><span>{copy}</span><Button size="sm" variant="ghost" onClick={onRetry} disabled={pending}><RefreshCw className={cn("h-3.5 w-3.5", pending && "animate-spin")} />{retry}</Button></div>;
}

function SubTab({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button type="button" onClick={onClick} className={cn("-mb-px border-b px-3 py-2.5 text-[11.5px] font-medium", active ? "border-text text-text" : "border-transparent text-text-muted hover:text-text")}>{children}</button>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="flex min-w-0 flex-col gap-1.5"><span className="text-[10.5px] font-medium text-text-muted">{label}</span>{children}</label>;
}
