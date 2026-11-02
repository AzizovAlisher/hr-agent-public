"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Archive,
  CheckCircle2,
  Clipboard,
  FileCheck2,
  FilePlus2,
  Link2,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  Settings2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, useTransition } from "react";
import { toast } from "sonner";

import type { components } from "@/lib/generated/api";
import type { CandidateSummary, Position } from "@/lib/types";
import { candRef, prjRef } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  approveAssessmentRetakeAction,
  createAssessmentInvitationAction,
  createProviderAssessmentInvitationAction,
  createAssessmentTemplateAction,
  gradeAssessmentAttemptAction,
  getAssessmentResultAction,
  listAssessmentProviderConnectionsAction,
  listAssessmentApplicationsAction,
  listAssessmentInvitationsAction,
  listAssessmentInvitationAttemptsAction,
  listProviderAssessmentEvidenceAction,
  pollProviderAssessmentResultAction,
  pollProviderAssessmentStatusAction,
  reissueAssessmentInvitationAction,
  revokeAssessmentInvitationAction,
  updateAssessmentTemplateAction,
} from "@/app/(app)/assessments/actions";
import { AssessmentVersionBuilder } from "@/components/assessments/assessment-version-builder";

type AssessmentTemplate = components["schemas"]["AssessmentTemplateOut"];
type AssessmentVersion = components["schemas"]["AssessmentVersionOut"];
type AssessmentInvitation = components["schemas"]["AssessmentInvitationOut"];
type AssessmentSubmission = components["schemas"]["AssessmentSubmissionOut"];
type AssessmentAttemptSummary = components["schemas"]["AssessmentAttemptSummaryOut"];
type Application = components["schemas"]["ApplicationOut"];
type ProviderConnection = components["schemas"]["IntegrationConnectionOut"];
type ProviderAssessmentEvidence =
  components["schemas"]["ProviderAssessmentEvidenceOut"];

/** @ui-only Assessment-admin workspace tab state. */
type View = "templates" | "invitations" | "results";

const COPY = {
  en: {
    tabs: { templates: "Templates", invitations: "Invitations", results: "Results" },
    newTemplate: "New template",
    templateName: "Template name",
    templateDescription: "Description",
    create: "Create template",
    creating: "Creating…",
    templatesEmpty: "No assessment templates yet.",
    active: "Active",
    archived: "Archived",
    archive: "Archive",
    restore: "Restore",
    versions: "Published versions",
    versionsEmpty: "No versions published yet.",
    version: "Version",
    questions: "questions",
    published: "Published",
    selectTemplate: "Select a template to edit and publish its first version.",
    invitationsTitle: "Candidate invitations",
    invitationHint:
      "Create a private link. The recipient email labels the invitation; delivery is handled outside Recruiter until a mail provider is connected.",
    application: "Application",
    chooseApplication: "Choose an application",
    assessmentVersion: "Assessment version",
    chooseVersion: "Choose a published version",
    recipient: "Recipient email (optional)",
    expiry: "Expires at",
    attempts: "Maximum attempts",
    extraTime: "Extra time (minutes)",
    breaksAllowed: "Breaks permitted (timer continues)",
    screenReaderOptimized: "Record a screen-reader support request",
    accommodations: "Accommodations",
    authorizedAttempts: "Authorized attempts",
    linkLanguage: "Link language",
    createLink: "Create private link",
    creatingLink: "Creating link…",
    oneTime: "Copy this one-time link now. The raw token is not stored in this interface.",
    copy: "Copy link",
    copied: "Private link copied",
    invitationHistory: "Invitation history",
    selectApplicationHint: "Select an application to load its invitation history.",
    noInvitations: "No invitations for this application.",
    loading: "Loading…",
    reload: "Reload",
    revoke: "Revoke",
    revoking: "Revoking…",
    reissue: "Reissue link",
    reissuing: "Reissuing…",
    reissueConfirm:
      "Replace this private link? The old link will stop working immediately.",
    reissued: "Replacement link created",
    approveRetake: "Approve next retake",
    approvingRetake: "Approving…",
    retakeApproved: "Retake approved",
    status: "Status",
    expires: "Expires",
    opened: "Opened",
    never: "Never",
    resultsTitle: "Assessment result",
    resultsHint:
      "Select an application and invitation to review every attempt, deterministic result, and submitted work sample.",
    chooseInvitation: "Choose an invitation",
    attemptsTitle: "Attempts",
    noAttempts: "No attempts have been started for this invitation.",
    openResult: "Open detailed result",
    downloadFile: "Download work sample",
    attemptId: "Attempt ID",
    loadResult: "Load result",
    score: "Score",
    passed: "Passed",
    notPassed: "Not passed",
    manualReview: "Manual review required",
    knockout: "Knockout condition matched",
    answers: "Saved answers",
    questionBreakdown: "Question breakdown",
    manualGrading: "Manual grading",
    manualGradingHint:
      "Score every manual question and record evidence for the decision. All entries are saved together.",
    manualScore: "Reviewer score",
    candidateAnswer: "Candidate answer",
    noCandidateAnswer: "No saved answer",
    evidenceComment: "Evidence comment",
    evidencePlaceholder: "Cite the submitted answer or work sample that supports this score.",
    saveManualGrades: "Save manual grades",
    savingManualGrades: "Saving grades…",
    manualGradeSaved: "Manual grades saved",
    manualGradeInvalid: "Enter a score within the question maximum and an evidence comment.",
    manualGradeReload:
      "Reload this result before retrying if another reviewer changed it.",
    reloadResult: "Reload result",
    knockoutSeparate:
      "Question scores and knockout adjudication remain separate evidence. Clearing a match never erases the original rule evidence.",
    knockoutReviewTitle: "Knockout adjudication",
    knockoutReviewHint:
      "Review the matched rule as a human decision. This action updates the assessment result but never rejects the candidate or changes the application stage.",
    knockoutMatchEvidence: "Matched rule evidence",
    knockoutOutcome: "Adjudication outcome",
    knockoutConfirmed: "Confirm the match (assessment remains not passed)",
    knockoutCleared: "Clear the match (score threshold applies)",
    knockoutReviewReason: "Decision reason",
    knockoutReviewPlaceholder:
      "Cite the source evidence that confirms or clears this match.",
    knockoutReviewInvalid: "Choose an outcome and record a decision reason.",
    knockoutReviewPending: "Awaiting human adjudication",
    knockoutReviewConfirmed: "Confirmed by a reviewer",
    knockoutReviewCleared: "Cleared by a reviewer",
    saveReview: "Save assessment review",
    savingReview: "Saving review…",
    reviewSaved: "Assessment review saved",
    relatedDataError: "Some application labels could not be loaded; stable IDs are shown instead.",
    loadMoreApplications: "Load more applications",
    loadingApplications: "Loading applications…",
    tokenPrefix: "Token prefix",
    providerTitle: "Specialist assessment provider",
    providerHint:
      "Invite through a connected specialist provider. Recruiters can use the connection without access to its credentials.",
    providerConnection: "Provider connection",
    chooseProvider: "Choose a connected provider",
    providerUnavailable:
      "No connected assessment provider is available. An integration administrator must connect one.",
    externalTemplate: "Provider template reference",
    providerInvite: "Send provider invitation",
    providerInviting: "Queuing invitation…",
    providerQueued: "Provider operation queued",
    providerHistory: "Provider invitation history",
    noProviderInvitations: "No specialist-provider invitations for this application.",
    refreshProviderStatus: "Refresh status",
    fetchProviderResult: "Fetch result",
    launchAssessment: "Open candidate assessment",
    providerScore: "Provider score",
    recommendation: "Recommendation",
    evidence: "Evidence",
    providerLoadFailed: "Provider assessment data could not be loaded.",
  },
  ru: {
    tabs: { templates: "Шаблоны", invitations: "Приглашения", results: "Результаты" },
    newTemplate: "Новый шаблон",
    templateName: "Название шаблона",
    templateDescription: "Описание",
    create: "Создать шаблон",
    creating: "Создание…",
    templatesEmpty: "Шаблонов оценивания пока нет.",
    active: "Активен",
    archived: "В архиве",
    archive: "Архивировать",
    restore: "Восстановить",
    versions: "Опубликованные версии",
    versionsEmpty: "Опубликованных версий пока нет.",
    version: "Версия",
    questions: "вопросов",
    published: "Опубликовано",
    selectTemplate: "Выберите шаблон, чтобы настроить и опубликовать первую версию.",
    invitationsTitle: "Приглашения кандидатов",
    invitationHint:
      "Создайте персональную ссылку. Email используется как метка; до подключения почтового провайдера ссылку нужно отправить самостоятельно.",
    application: "Отклик",
    chooseApplication: "Выберите отклик",
    assessmentVersion: "Версия оценивания",
    chooseVersion: "Выберите опубликованную версию",
    recipient: "Email получателя (необязательно)",
    expiry: "Действует до",
    attempts: "Максимум попыток",
    extraTime: "Дополнительное время (минуты)",
    breaksAllowed: "Перерывы разрешены (таймер продолжает идти)",
    screenReaderOptimized: "Зафиксировать запрос на поддержку программы чтения с экрана",
    accommodations: "Адаптации",
    authorizedAttempts: "Разрешено попыток",
    linkLanguage: "Язык ссылки",
    createLink: "Создать персональную ссылку",
    creatingLink: "Создание ссылки…",
    oneTime: "Скопируйте одноразовую ссылку сейчас. Исходный токен не хранится в интерфейсе.",
    copy: "Копировать ссылку",
    copied: "Персональная ссылка скопирована",
    invitationHistory: "История приглашений",
    selectApplicationHint: "Выберите отклик, чтобы загрузить историю приглашений.",
    noInvitations: "Для этого отклика приглашений нет.",
    loading: "Загрузка…",
    reload: "Обновить",
    revoke: "Отозвать",
    revoking: "Отзыв…",
    reissue: "Перевыпустить ссылку",
    reissuing: "Перевыпуск…",
    reissueConfirm:
      "Заменить персональную ссылку? Старая ссылка сразу перестанет работать.",
    reissued: "Новая ссылка создана",
    approveRetake: "Разрешить следующую попытку",
    approvingRetake: "Разрешение…",
    retakeApproved: "Повторная попытка разрешена",
    status: "Статус",
    expires: "Истекает",
    opened: "Открыто",
    never: "Никогда",
    resultsTitle: "Результат оценивания",
    resultsHint:
      "Выберите отклик и приглашение, чтобы проверить все попытки, итоговую оценку и отправленные файлы.",
    chooseInvitation: "Выберите приглашение",
    attemptsTitle: "Попытки",
    noAttempts: "По этому приглашению ещё нет начатых попыток.",
    openResult: "Открыть подробный результат",
    downloadFile: "Скачать отправленный файл",
    attemptId: "ID попытки",
    loadResult: "Загрузить результат",
    score: "Результат",
    passed: "Пройдено",
    notPassed: "Не пройдено",
    manualReview: "Требуется ручная проверка",
    knockout: "Сработало отсеивающее условие",
    answers: "Сохранённые ответы",
    questionBreakdown: "Разбивка по вопросам",
    manualGrading: "Ручная оценка",
    manualGradingHint:
      "Оцените каждый ручной вопрос и зафиксируйте обоснование. Все записи сохраняются вместе.",
    manualScore: "Оценка проверяющего",
    candidateAnswer: "Ответ кандидата",
    noCandidateAnswer: "Сохранённого ответа нет",
    evidenceComment: "Обоснование",
    evidencePlaceholder: "Укажите ответ или файл, который подтверждает эту оценку.",
    saveManualGrades: "Сохранить ручные оценки",
    savingManualGrades: "Сохранение оценок…",
    manualGradeSaved: "Ручные оценки сохранены",
    manualGradeInvalid: "Укажите оценку в допустимых пределах и добавьте обоснование.",
    manualGradeReload:
      "Если результат изменил другой проверяющий, обновите его перед повторной попыткой.",
    reloadResult: "Обновить результат",
    knockoutSeparate:
      "Оценки вопросов и решение по отсеивающему условию хранятся раздельно. Снятие условия не удаляет исходное срабатывание.",
    knockoutReviewTitle: "Решение по отсеивающему условию",
    knockoutReviewHint:
      "Проверьте срабатывание как человек. Действие обновит результат оценивания, но не отклонит кандидата и не изменит этап отклика.",
    knockoutMatchEvidence: "Данные сработавшего правила",
    knockoutOutcome: "Решение проверяющего",
    knockoutConfirmed: "Подтвердить срабатывание (оценивание не пройдено)",
    knockoutCleared: "Снять срабатывание (учитывать проходной балл)",
    knockoutReviewReason: "Обоснование решения",
    knockoutReviewPlaceholder:
      "Укажите источник данных, подтверждающий или снимающий срабатывание.",
    knockoutReviewInvalid: "Выберите решение и добавьте обоснование.",
    knockoutReviewPending: "Ожидает решения проверяющего",
    knockoutReviewConfirmed: "Подтверждено проверяющим",
    knockoutReviewCleared: "Снято проверяющим",
    saveReview: "Сохранить проверку оценивания",
    savingReview: "Сохранение проверки…",
    reviewSaved: "Проверка оценивания сохранена",
    relatedDataError: "Часть подписей не загрузилась; вместо них показаны стабильные ID.",
    loadMoreApplications: "Загрузить ещё отклики",
    loadingApplications: "Загрузка откликов…",
    tokenPrefix: "Префикс токена",
    providerTitle: "Оценивание у специализированного провайдера",
    providerHint:
      "Отправьте приглашение через подключённого провайдера. Рекрутер может использовать подключение без доступа к его учётным данным.",
    providerConnection: "Подключение провайдера",
    chooseProvider: "Выберите подключённого провайдера",
    providerUnavailable:
      "Подключённый провайдер оценивания недоступен. Администратор интеграций должен его настроить.",
    externalTemplate: "Идентификатор шаблона у провайдера",
    providerInvite: "Отправить приглашение провайдера",
    providerInviting: "Приглашение ставится в очередь…",
    providerQueued: "Операция провайдера поставлена в очередь",
    providerHistory: "История приглашений провайдера",
    noProviderInvitations: "Для этого отклика нет приглашений внешнего провайдера.",
    refreshProviderStatus: "Обновить статус",
    fetchProviderResult: "Получить результат",
    launchAssessment: "Открыть тестирование кандидата",
    providerScore: "Результат провайдера",
    recommendation: "Рекомендация",
    evidence: "Подтверждения",
    providerLoadFailed: "Не удалось загрузить данные оценивания провайдера.",
  },
} as const;

function currentView(
  value: string | null,
  access: { canManage: boolean; canReview: boolean },
): View {
  if (value === "results" && access.canReview) return "results";
  if ((value === "templates" || value === "invitations") && access.canManage) {
    return value;
  }
  return access.canManage ? "templates" : "results";
}

function queryHref(view: View, params: URLSearchParams, extra?: Record<string, string | null>) {
  const next = new URLSearchParams(params);
  next.set("view", view);
  for (const [key, value] of Object.entries(extra ?? {})) {
    if (value) next.set(key, value);
    else next.delete(key);
  }
  return `/assessments?${next.toString()}`;
}

function localDateTime(date: Date): string {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function localExpiry(): string {
  return localDateTime(new Date(Date.now() + 7 * 24 * 60 * 60 * 1_000));
}

export function AdminAssessmentWorkspace({
  initialTemplates,
  initialApplications,
  initialApplicationsNextCursor,
  candidates,
  positions,
  language,
  draftScope,
  canManage,
  canReview,
  accessError,
  relatedDataError,
}: {
  initialTemplates: AssessmentTemplate[];
  initialApplications: Application[];
  initialApplicationsNextCursor: string | null;
  candidates: CandidateSummary[];
  positions: Position[];
  language: "en" | "ru";
  draftScope: string;
  canManage: boolean;
  canReview: boolean;
  accessError: string | null;
  relatedDataError: boolean;
}) {
  const t = COPY[language];
  const searchParams = useSearchParams();
  const view = currentView(searchParams.get("view"), { canManage, canReview });
  const [templates, setTemplates] = useState(initialTemplates);
  const [applications, setApplications] = useState(initialApplications);
  const [applicationsNextCursor, setApplicationsNextCursor] = useState(
    initialApplicationsNextCursor,
  );
  const [applicationsLoading, setApplicationsLoading] = useState(false);
  const selectedTemplateId =
    searchParams.get("template") ?? templates.find((template) => template.status === "active")?.id;
  const selectedTemplate = templates.find((template) => template.id === selectedTemplateId) ?? null;

  async function loadMoreApplications() {
    if (applicationsNextCursor == null || applicationsLoading) return;
    setApplicationsLoading(true);
    const result = await listAssessmentApplicationsAction(applicationsNextCursor);
    if (result.ok) {
      setApplications((current) => {
        const known = new Set(current.map((application) => application.id));
        return [
          ...current,
          ...result.data.items.filter((application) => !known.has(application.id)),
        ];
      });
      setApplicationsNextCursor(result.data.next_cursor);
    } else {
      toast.error(result.error);
    }
    setApplicationsLoading(false);
  }

  if (accessError) {
    return (
      <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 p-5 text-sm text-destructive">
        {accessError}
      </div>
    );
  }

  return (
    <div>
      {relatedDataError ? (
        <p role="status" className="mb-4 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
          {t.relatedDataError}
        </p>
      ) : null}
      <nav aria-label={language === "ru" ? "Раздел оцениваний" : "Assessment section"} className="mb-6 flex gap-1 overflow-x-auto border-b border-border">
        {(
          [
            ...(canManage
              ? ([
                  ["templates", t.tabs.templates, Settings2],
                  ["invitations", t.tabs.invitations, Send],
                ] as const)
              : []),
            ...(canReview
              ? ([["results", t.tabs.results, FileCheck2]] as const)
              : []),
          ] as ReadonlyArray<readonly [View, string, typeof Settings2]>
        ).map(([key, label, Icon]) => (
          <Link
            key={key}
            id={`assessment-tab-${key}`}
            aria-current={view === key ? "page" : undefined}
            href={queryHref(key, searchParams)}
            className={cn(
              "flex min-h-10 shrink-0 items-center gap-2 border-b-2 px-3 py-2 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring",
              view === key
                ? "border-primary text-text"
                : "border-transparent text-text-muted hover:text-text",
            )}
          >
            <Icon className="h-4 w-4" aria-hidden />
            {label}
          </Link>
        ))}
      </nav>

      <section
        id={`assessment-panel-${view}`}
        tabIndex={0}
        className="outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {view === "templates" ? (
          <TemplatesPanel
            templates={templates}
            selected={selectedTemplate}
            searchParams={searchParams}
            language={language}
            draftScope={draftScope}
            onTemplatesChange={setTemplates}
          />
        ) : view === "invitations" ? (
          <InvitationsPanel
            templates={templates}
            applications={applications}
            applicationsHaveMore={applicationsNextCursor != null}
            applicationsLoading={applicationsLoading}
            onLoadMoreApplications={loadMoreApplications}
            candidates={candidates}
            positions={positions}
            language={language}
            searchParams={searchParams}
          />
        ) : (
          <ResultsPanel
            applications={applications}
            applicationsHaveMore={applicationsNextCursor != null}
            applicationsLoading={applicationsLoading}
            onLoadMoreApplications={loadMoreApplications}
            candidates={candidates}
            positions={positions}
            language={language}
            searchParams={searchParams}
          />
        )}
      </section>
    </div>
  );
}

function TemplatesPanel({
  templates,
  selected,
  searchParams,
  language,
  draftScope,
  onTemplatesChange,
}: {
  templates: AssessmentTemplate[];
  selected: AssessmentTemplate | null;
  searchParams: URLSearchParams;
  language: "en" | "ru";
  draftScope: string;
  onTemplatesChange: (templates: AssessmentTemplate[]) => void;
}) {
  const t = COPY[language];
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [pending, startTransition] = useTransition();
  const [statusPending, startStatusTransition] = useTransition();

  function createTemplate() {
    startTransition(async () => {
      const result = await createAssessmentTemplateAction({
        name,
        description: description.trim() || null,
      });
      if (!result.ok) {
        toast.error(result.error);
        return;
      }
      onTemplatesChange([...templates, { ...result.data, versions: [] }]);
      setName("");
      setDescription("");
      toast.success(language === "ru" ? "Шаблон создан" : "Template created");
    });
  }

  function toggleStatus(template: AssessmentTemplate) {
    startStatusTransition(async () => {
      const nextStatus = template.status === "active" ? "archived" : "active";
      const result = await updateAssessmentTemplateAction(template.id, { status: nextStatus });
      if (!result.ok) {
        toast.error(result.error);
        return;
      }
      onTemplatesChange(
        templates.map((item) =>
          item.id === template.id ? { ...item, ...result.data, versions: item.versions } : item,
        ),
      );
      toast.success(nextStatus === "active" ? t.active : t.archived);
    });
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[300px_minmax(0,1fr)]">
      <aside className="space-y-4">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            createTemplate();
          }}
          className="rounded-xl border border-border bg-bg-elevated p-4"
        >
          <h2 className="font-semibold">{t.newTemplate}</h2>
          <label className="mt-4 block text-xs font-medium text-text-muted" htmlFor="assessment-template-name">
            {t.templateName}
          </label>
          <input
            id="assessment-template-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={160}
            className="mt-1 min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
          />
          <label className="mt-3 block text-xs font-medium text-text-muted" htmlFor="assessment-template-description">
            {t.templateDescription}
          </label>
          <textarea
            id="assessment-template-description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            maxLength={10_000}
            rows={3}
            className="mt-1 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
          />
          <button
            type="submit"
            disabled={pending || !name.trim()}
            className="mt-3 flex min-h-10 w-full items-center justify-center gap-2 rounded-lg bg-primary px-3 py-2 text-sm font-semibold text-primary-foreground focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
          >
            {pending ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Plus className="h-4 w-4" />}
            {pending ? t.creating : t.create}
          </button>
        </form>

        <nav aria-label={t.tabs.templates} className="space-y-2">
          {templates.length === 0 ? (
            <p className="rounded-xl border border-dashed border-border p-4 text-sm text-text-muted">
              {t.templatesEmpty}
            </p>
          ) : (
            templates.map((template) => (
              <Link
                key={template.id}
                href={queryHref("templates", searchParams, { template: template.id })}
                aria-current={selected?.id === template.id ? "page" : undefined}
                className={cn(
                  "block rounded-xl border p-3 outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring",
                  selected?.id === template.id
                    ? "border-primary/50 bg-accent-muted"
                    : "border-border bg-bg-elevated hover:bg-bg-hover",
                )}
              >
                <span className="flex items-start justify-between gap-2">
                  <span className="min-w-0 truncate text-sm font-semibold">{template.name}</span>
                  <StatusPill status={template.status} language={language} />
                </span>
                <span className="mt-1 block text-xs text-text-muted">
                  {(template.versions ?? []).length} {t.versions.toLowerCase()}
                </span>
              </Link>
            ))
          )}
        </nav>
      </aside>

      {selected ? (
        <div className="min-w-0 space-y-5">
          <section className="rounded-xl border border-border bg-bg-elevated p-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="flex items-center gap-2">
                  <h2 className="text-xl font-semibold tracking-tight">{selected.name}</h2>
                  <StatusPill status={selected.status} language={language} />
                </div>
                {selected.description ? (
                  <p className="mt-2 max-w-3xl text-sm leading-6 text-text-muted">{selected.description}</p>
                ) : null}
              </div>
              <button
                type="button"
                onClick={() => toggleStatus(selected)}
                disabled={statusPending}
                className="flex min-h-9 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
              >
                {statusPending ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : selected.status === "active" ? <Archive className="h-4 w-4" /> : <RotateCcw className="h-4 w-4" />}
                {selected.status === "active" ? t.archive : t.restore}
              </button>
            </div>
            <div className="mt-5 border-t border-border pt-4">
              <h3 className="text-sm font-semibold">{t.versions}</h3>
              {(selected.versions ?? []).length === 0 ? (
                <p className="mt-2 text-sm text-text-muted">{t.versionsEmpty}</p>
              ) : (
                <ol className="mt-3 space-y-2">
                  {[...(selected.versions ?? [])]
                    .sort((a, b) => b.version - a.version)
                    .map((version) => (
                      <VersionSummary key={version.id} version={version} language={language} />
                    ))}
                </ol>
              )}
            </div>
          </section>
          {selected.status === "active" ? (
            <AssessmentVersionBuilder
              key={selected.id}
              template={selected}
              language={language}
              draftScope={draftScope}
              onPublished={(version) =>
                onTemplatesChange(
                  templates.map((template) =>
                    template.id === selected.id
                      ? { ...template, versions: [...(template.versions ?? []), version] }
                      : template,
                  ),
                )
              }
            />
          ) : null}
        </div>
      ) : (
        <div className="rounded-xl border border-dashed border-border p-8 text-center text-sm text-text-muted">
          <FilePlus2 className="mx-auto mb-3 h-7 w-7" aria-hidden />
          {t.selectTemplate}
        </div>
      )}
    </div>
  );
}

function InvitationsPanel({
  templates,
  applications,
  applicationsHaveMore,
  applicationsLoading,
  onLoadMoreApplications,
  candidates,
  positions,
  language,
  searchParams,
}: {
  templates: AssessmentTemplate[];
  applications: Application[];
  applicationsHaveMore: boolean;
  applicationsLoading: boolean;
  onLoadMoreApplications: () => void | Promise<void>;
  candidates: CandidateSummary[];
  positions: Position[];
  language: "en" | "ru";
  searchParams: URLSearchParams;
}) {
  const t = COPY[language];
  const router = useRouter();
  const candidateMap = useMemo(() => new Map(candidates.map((item) => [item.id, item])), [candidates]);
  const positionMap = useMemo(() => new Map(positions.map((item) => [item.id, item])), [positions]);
  const versions = templates.flatMap((template) =>
    (template.versions ?? []).map((version) => ({ template, version })),
  );
  const selectedApplication =
    searchParams.get("application") ?? applications[0]?.id ?? "";
  const [applicationId, setApplicationId] = useState(selectedApplication);
  const [versionId, setVersionId] = useState(versions[0]?.version.id ?? "");
  const [email, setEmail] = useState("");
  const [expiry, setExpiry] = useState(localExpiry);
  const [maxAttempts, setMaxAttempts] = useState(1);
  const [extraTime, setExtraTime] = useState(0);
  const [breaksAllowed, setBreaksAllowed] = useState(false);
  const [screenReaderOptimized, setScreenReaderOptimized] = useState(false);
  const [linkLanguage, setLinkLanguage] = useState<"en" | "ru">(language);
  const [invitations, setInvitations] = useState<AssessmentInvitation[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [createdLink, setCreatedLink] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [reissuingId, setReissuingId] = useState<string | null>(null);
  const invitationRequestGeneration = useRef(0);

  async function loadInvitations(id = applicationId) {
    const generation = ++invitationRequestGeneration.current;
    if (!id) {
      setInvitations([]);
      setLoadError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setLoadError(null);
    const result = await listAssessmentInvitationsAction(id);
    if (generation !== invitationRequestGeneration.current) return;
    if (result.ok) setInvitations(result.data);
    else setLoadError(result.error);
    setLoading(false);
  }

  useEffect(() => {
    if (applicationId) void loadInvitations(applicationId);
    else {
      invitationRequestGeneration.current += 1;
      setInvitations([]);
      setLoadError(null);
      setLoading(false);
    }
    // The selected application is the only trigger; the action is server-authorized.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applicationId]);

  function createInvitation() {
    if (!applicationId || !versionId || !expiry) return;
    startTransition(async () => {
      const expiresAt = new Date(expiry);
      const result = await createAssessmentInvitationAction(applicationId, {
        template_version_id: versionId,
        expires_at: expiresAt.toISOString(),
        max_attempts: maxAttempts,
        recipient_email: email.trim() || null,
        accommodations: {
          extra_time_minutes: extraTime,
          breaks_allowed: breaksAllowed,
          screen_reader_optimized: screenReaderOptimized,
        },
      });
      if (!result.ok) {
        toast.error(result.error);
        return;
      }
      const { token, ...safeInvitation } = result.data;
      setInvitations((current) => [safeInvitation, ...current]);
      setCreatedLink(
        `${window.location.origin}/assessment/launch/${encodeURIComponent(token)}?lang=${linkLanguage}`,
      );
      setEmail("");
      toast.success(language === "ru" ? "Ссылка создана" : "Private link created");
    });
  }

  async function revoke(invitationId: string) {
    setRevokingId(invitationId);
    const result = await revokeAssessmentInvitationAction(invitationId);
    if (result.ok) {
      setInvitations((current) =>
        current.map((item) => (item.id === invitationId ? result.data : item)),
      );
    } else toast.error(result.error);
    setRevokingId(null);
  }

  async function reissue(invitationId: string) {
    if (!window.confirm(t.reissueConfirm)) return;
    setReissuingId(invitationId);
    const expiresAt = new Date(Date.now() + 7 * 24 * 60 * 60 * 1_000);
    const result = await reissueAssessmentInvitationAction(
      invitationId,
      expiresAt.toISOString(),
    );
    if (result.ok) {
      const { token, ...safeInvitation } = result.data;
      setInvitations((current) =>
        current.map((item) =>
          item.id === invitationId ? safeInvitation : item,
        ),
      );
      setCreatedLink(
        `${window.location.origin}/assessment/launch/${encodeURIComponent(token)}?lang=${linkLanguage}`,
      );
      toast.success(t.reissued);
    } else {
      toast.error(result.error);
    }
    setReissuingId(null);
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-6 xl:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <h2 className="text-lg font-semibold">{t.invitationsTitle}</h2>
        <p className="mt-2 text-sm leading-6 text-text-muted">{t.invitationHint}</p>
        <div className="mt-5 space-y-4">
          <FieldLabel label={t.application} htmlFor="assessment-application">
            <select
              id="assessment-application"
              value={applicationId}
              onChange={(event) => {
                setApplicationId(event.target.value);
                setCreatedLink(null);
                router.replace(
                  queryHref("invitations", searchParams, {
                    application: event.target.value || null,
                  }),
                  { scroll: false },
                );
              }}
              className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
            >
              <option value="">{t.chooseApplication}</option>
              {applications.map((application) => (
                <option key={application.id} value={application.id}>
                  {applicationLabel(application, candidateMap, positionMap)}
                </option>
              ))}
            </select>
          </FieldLabel>
          <ApplicationLoadMore
            hasMore={applicationsHaveMore}
            loading={applicationsLoading}
            onLoadMore={onLoadMoreApplications}
            language={language}
          />
          <FieldLabel label={t.assessmentVersion} htmlFor="assessment-version">
            <select
              id="assessment-version"
              value={versionId}
              onChange={(event) => setVersionId(event.target.value)}
              className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
            >
              <option value="">{t.chooseVersion}</option>
              {versions.map(({ template, version }) => (
                <option key={version.id} value={version.id}>
                  {template.name} · v{version.version} · {version.title}
                </option>
              ))}
            </select>
          </FieldLabel>
          <FieldLabel label={t.recipient} htmlFor="assessment-recipient">
            <input
              id="assessment-recipient"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
              className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
            />
          </FieldLabel>
          <div className="grid gap-4 sm:grid-cols-2">
            <FieldLabel label={t.expiry} htmlFor="assessment-expiry">
              <input
                id="assessment-expiry"
                type="datetime-local"
                value={expiry}
                min={localDateTime(new Date())}
                onChange={(event) => setExpiry(event.target.value)}
                required
                className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
              />
            </FieldLabel>
            <FieldLabel label={t.attempts} htmlFor="assessment-max-attempts">
              <input
                id="assessment-max-attempts"
                type="number"
                min={1}
                max={20}
                value={maxAttempts}
                onChange={(event) => setMaxAttempts(Number(event.target.value))}
                className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
              />
            </FieldLabel>
          </div>
          <fieldset className="rounded-xl border border-border p-3">
            <legend className="px-1 text-xs font-medium text-text-muted">
              {t.accommodations}
            </legend>
            <FieldLabel label={t.extraTime} htmlFor="assessment-extra-time">
              <input
                id="assessment-extra-time"
                type="number"
                min={0}
                max={1_440}
                value={extraTime}
                onChange={(event) => setExtraTime(Number(event.target.value))}
                className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
              />
            </FieldLabel>
            <div className="mt-3 grid gap-2">
              <label className="flex min-h-10 cursor-pointer items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={breaksAllowed}
                  onChange={(event) => setBreaksAllowed(event.target.checked)}
                  className="h-4 w-4 accent-primary"
                />
                {t.breaksAllowed}
              </label>
              <label className="flex min-h-10 cursor-pointer items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={screenReaderOptimized}
                  onChange={(event) => setScreenReaderOptimized(event.target.checked)}
                  className="h-4 w-4 accent-primary"
                />
                {t.screenReaderOptimized}
              </label>
            </div>
          </fieldset>
          <fieldset>
            <legend className="text-xs font-medium text-text-muted">{t.linkLanguage}</legend>
            <div className="mt-1 grid grid-cols-2 gap-2">
              {(["en", "ru"] as const).map((item) => (
                <label key={item} className="flex min-h-10 cursor-pointer items-center justify-center gap-2 rounded-lg border border-border px-3 py-2 text-sm uppercase focus-within:ring-3 focus-within:ring-ring">
                  <input
                    type="radio"
                    name="assessment-link-language"
                    checked={linkLanguage === item}
                    onChange={() => setLinkLanguage(item)}
                    className="accent-primary"
                  />
                  {item}
                </label>
              ))}
            </div>
          </fieldset>
          <button
            type="button"
            onClick={createInvitation}
            disabled={pending || !applicationId || !versionId || !expiry}
            className="flex min-h-11 w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
          >
            {pending ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Link2 className="h-4 w-4" />}
            {pending ? t.creatingLink : t.createLink}
          </button>
        </div>
        {createdLink ? (
          <div className="mt-5 rounded-xl border border-success/30 bg-success/10 p-4">
            <p className="text-sm font-medium text-success">{t.oneTime}</p>
            <div className="mt-3 flex flex-col gap-2 sm:flex-row">
              <input
                readOnly
                value={createdLink}
                aria-label={t.oneTime}
                className="min-h-10 min-w-0 flex-1 rounded-lg border border-border bg-bg px-3 py-2 font-mono text-xs"
                onFocus={(event) => event.currentTarget.select()}
              />
              <button
                type="button"
                onClick={async () => {
                  await navigator.clipboard.writeText(createdLink);
                  toast.success(t.copied);
                }}
                className="flex min-h-10 items-center justify-center gap-2 rounded-lg border border-border bg-bg px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring"
              >
                <Clipboard className="h-4 w-4" />
                {t.copy}
              </button>
            </div>
          </div>
        ) : null}
        </section>

        <section className="min-w-0 rounded-xl border border-border bg-bg-elevated p-5">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">{t.invitationHistory}</h2>
          <button
            type="button"
            onClick={() => void loadInvitations()}
            disabled={!applicationId || loading}
            aria-label={t.reload}
            className="flex min-h-9 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
          >
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin motion-reduce:animate-none")} />
            {t.reload}
          </button>
        </div>
        {!applicationId ? (
          <p className="mt-5 text-sm text-text-muted">{t.selectApplicationHint}</p>
        ) : loading ? (
          <p role="status" className="mt-5 flex items-center gap-2 text-sm text-text-muted">
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> {t.loading}
          </p>
        ) : loadError ? (
          <p role="alert" className="mt-5 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{loadError}</p>
        ) : invitations.length === 0 ? (
          <p className="mt-5 text-sm text-text-muted">{t.noInvitations}</p>
        ) : (
          <ul className="mt-4 space-y-3">
            {invitations.map((invitation) => (
              <li key={invitation.id} className="rounded-xl border border-border bg-bg p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <StatusPill status={invitation.status} language={language} />
                    <p className="mt-2 text-sm font-medium">
                      {invitation.recipient_email || `${t.tokenPrefix}: ${invitation.token_prefix}…`}
                    </p>
                  </div>
                  {invitation.status === "active" || invitation.status === "expired" ? (
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => void reissue(invitation.id)}
                        disabled={reissuingId === invitation.id || revokingId === invitation.id}
                        className="flex min-h-9 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
                      >
                        {reissuingId === invitation.id ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <RotateCcw className="h-4 w-4" />}
                        {reissuingId === invitation.id ? t.reissuing : t.reissue}
                      </button>
                      {invitation.status === "active" ? (
                        <button
                          type="button"
                          onClick={() => void revoke(invitation.id)}
                          disabled={revokingId === invitation.id || reissuingId === invitation.id}
                          className="flex min-h-9 items-center gap-2 rounded-lg border border-destructive/30 px-3 py-2 text-sm font-medium text-destructive hover:bg-destructive/10 focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
                        >
                          {revokingId === invitation.id ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Archive className="h-4 w-4" />}
                          {revokingId === invitation.id ? t.revoking : t.revoke}
                        </button>
                      ) : null}
                    </div>
                  ) : invitation.status === "completed" ? (
                    <Link
                      href={queryHref("results", searchParams, {
                        application: applicationId,
                        invitation: invitation.id,
                      })}
                      className="flex min-h-9 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring"
                    >
                      <FileCheck2 className="h-4 w-4" />
                      {t.tabs.results}
                    </Link>
                  ) : null}
                </div>
                <dl className="mt-3 grid gap-2 text-xs text-text-muted sm:grid-cols-2">
                  <InfoRow label={t.expires} value={formatDate(invitation.expires_at, language)} />
                  <InfoRow label={t.opened} value={invitation.first_opened_at ? formatDate(invitation.first_opened_at, language) : t.never} />
                  <InfoRow
                    label={t.authorizedAttempts}
                    value={`${invitation.authorized_attempts}/${invitation.max_attempts}`}
                  />
                  <InfoRow
                    label={t.extraTime}
                    value={`${invitation.accommodations?.extra_time_minutes ?? 0}`}
                  />
                </dl>
              </li>
            ))}
          </ul>
        )}
        </section>
      </div>
      <ProviderAssessmentPanel
        mode="manage"
        applicationId={applicationId}
        templateVersionId={versionId || null}
        language={language}
      />
    </div>
  );
}

function ProviderAssessmentPanel({
  mode,
  applicationId,
  templateVersionId = null,
  language,
}: {
  mode: "manage" | "review";
  applicationId: string;
  templateVersionId?: string | null;
  language: "en" | "ru";
}) {
  const t = COPY[language];
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [externalTemplateRef, setExternalTemplateRef] = useState("");
  const [expiry, setExpiry] = useState(localExpiry);
  const [extraTimeMinutes, setExtraTimeMinutes] = useState(0);
  const [screenReaderOptimized, setScreenReaderOptimized] = useState(false);
  const [items, setItems] = useState<ProviderAssessmentEvidence[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const evidenceGeneration = useRef(0);

  const connected = connections.filter(
    (connection) => connection.status === "connected",
  );

  useEffect(() => {
    let active = true;
    void listAssessmentProviderConnectionsAction().then((result) => {
      if (!active) return;
      if (result.ok) {
        setConnections(result.data.items);
        const firstConnected = result.data.items.find(
          (connection) => connection.status === "connected",
        );
        setConnectionId((current) => current || firstConnected?.id || "");
      } else {
        setLoadError(result.error);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  async function loadEvidence(id = applicationId) {
    const generation = ++evidenceGeneration.current;
    if (!id) {
      setItems([]);
      setLoadError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setLoadError(null);
    const result = await listProviderAssessmentEvidenceAction(id);
    if (generation !== evidenceGeneration.current) return;
    if (result.ok) setItems(result.data.items);
    else {
      setItems([]);
      setLoadError(result.error);
    }
    setLoading(false);
  }

  useEffect(() => {
    void loadEvidence(applicationId);
    // The selected application is the only evidence boundary.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applicationId]);

  async function invite() {
    if (!applicationId || !connectionId || !externalTemplateRef.trim() || !expiry) {
      return;
    }
    setPendingKey("invite");
    const result = await createProviderAssessmentInvitationAction(connectionId, {
      application_id: applicationId,
      template_version_id: templateVersionId,
      external_template_ref: externalTemplateRef.trim(),
      expires_at: new Date(expiry).toISOString(),
      locale: language,
      accommodations: {
        extra_time_minutes: extraTimeMinutes,
        screen_reader_optimized: screenReaderOptimized,
      },
      idempotency_key: `assessment-provider-invite:${crypto.randomUUID()}`,
    });
    if (result.ok) {
      toast.success(t.providerQueued);
      setExternalTemplateRef("");
      await loadEvidence();
    } else {
      toast.error(result.error);
    }
    setPendingKey(null);
  }

  async function poll(
    item: ProviderAssessmentEvidence,
    operation: "status" | "result",
  ) {
    const key = `${item.id}:${operation}`;
    setPendingKey(key);
    const input = {
      provider_assessment_id: item.id,
      idempotency_key: `assessment-provider-${operation}:${crypto.randomUUID()}`,
    };
    const response =
      operation === "status"
        ? await pollProviderAssessmentStatusAction(item.connection_id, input)
        : await pollProviderAssessmentResultAction(item.connection_id, input);
    if (response.ok) toast.success(t.providerQueued);
    else toast.error(response.error);
    setPendingKey(null);
  }

  return (
    <section className="rounded-xl border border-border bg-bg-elevated p-5 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">{t.providerTitle}</h2>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-text-muted">
            {t.providerHint}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void loadEvidence()}
          disabled={!applicationId || loading}
          className="flex min-h-10 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
        >
          <RefreshCw
            className={cn(
              "h-4 w-4",
              loading && "animate-spin motion-reduce:animate-none",
            )}
          />
          {t.reload}
        </button>
      </div>

      {mode === "manage" ? (
        connected.length === 0 ? (
          <p role="status" className="mt-5 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
            {t.providerUnavailable}
          </p>
        ) : (
          <div className="mt-5 grid gap-4 lg:grid-cols-2">
            <FieldLabel label={t.providerConnection} htmlFor="assessment-provider-connection">
              <select
                id="assessment-provider-connection"
                value={connectionId}
                onChange={(event) => setConnectionId(event.target.value)}
                className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
              >
                <option value="">{t.chooseProvider}</option>
                {connected.map((connection) => (
                  <option key={connection.id} value={connection.id}>
                    {providerConnectionLabel(connection)}
                  </option>
                ))}
              </select>
            </FieldLabel>
            <FieldLabel label={t.externalTemplate} htmlFor="assessment-provider-template">
              <input
                id="assessment-provider-template"
                value={externalTemplateRef}
                onChange={(event) => setExternalTemplateRef(event.target.value)}
                maxLength={255}
                className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
              />
            </FieldLabel>
            <FieldLabel label={t.expiry} htmlFor="assessment-provider-expiry">
              <input
                id="assessment-provider-expiry"
                type="datetime-local"
                value={expiry}
                min={localDateTime(new Date())}
                onChange={(event) => setExpiry(event.target.value)}
                className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
              />
            </FieldLabel>
            <div className="grid gap-2 sm:grid-cols-2">
              <FieldLabel label={t.extraTime} htmlFor="assessment-provider-extra-time">
                <input
                  id="assessment-provider-extra-time"
                  type="number"
                  min={0}
                  max={1_440}
                  value={extraTimeMinutes}
                  onChange={(event) => setExtraTimeMinutes(Number(event.target.value))}
                  className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
                />
              </FieldLabel>
              <label className="mt-5 flex min-h-10 cursor-pointer items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={screenReaderOptimized}
                  onChange={(event) => setScreenReaderOptimized(event.target.checked)}
                  className="h-4 w-4 accent-primary"
                />
                {t.screenReaderOptimized}
              </label>
            </div>
            <button
              type="button"
              onClick={() => void invite()}
              disabled={
                pendingKey === "invite" ||
                !applicationId ||
                !connectionId ||
                !externalTemplateRef.trim() ||
                !expiry
              }
              className="flex min-h-11 items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50 lg:col-span-2"
            >
              {pendingKey === "invite" ? (
                <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
              ) : (
                <Send className="h-4 w-4" />
              )}
              {pendingKey === "invite" ? t.providerInviting : t.providerInvite}
            </button>
          </div>
        )
      ) : null}

      <div className="mt-6 border-t border-border pt-5">
        <h3 className="font-semibold">{t.providerHistory}</h3>
        {!applicationId ? (
          <p className="mt-3 text-sm text-text-muted">{t.selectApplicationHint}</p>
        ) : loading ? (
          <p role="status" className="mt-3 flex items-center gap-2 text-sm text-text-muted">
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
            {t.loading}
          </p>
        ) : loadError ? (
          <p role="alert" className="mt-3 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
            {t.providerLoadFailed} {loadError}
          </p>
        ) : items.length === 0 ? (
          <p className="mt-3 text-sm text-text-muted">{t.noProviderInvitations}</p>
        ) : (
          <ul className="mt-3 space-y-3">
            {items.map((item) => (
              <li key={item.id} className="rounded-xl border border-border bg-bg p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusPill status={item.status} language={language} />
                      <span className="text-xs text-text-muted">
                        {providerConnectionLabel(
                          connections.find((connection) => connection.id === item.connection_id),
                        )}
                      </span>
                    </div>
                    <p className="mt-2 font-mono text-xs text-text-muted">
                      {item.external_template_ref}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {mode === "manage" && !["graded", "expired", "cancelled", "failed"].includes(item.status) ? (
                      <button
                        type="button"
                        onClick={() => void poll(item, "status")}
                        disabled={pendingKey === `${item.id}:status`}
                        className="flex min-h-9 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
                      >
                        {pendingKey === `${item.id}:status` ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <RefreshCw className="h-4 w-4" />}
                        {t.refreshProviderStatus}
                      </button>
                    ) : null}
                    {mode === "review" && ["started", "submitted", "graded"].includes(item.status) ? (
                      <button
                        type="button"
                        onClick={() => void poll(item, "result")}
                        disabled={pendingKey === `${item.id}:result`}
                        className="flex min-h-9 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
                      >
                        {pendingKey === `${item.id}:result` ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <FileCheck2 className="h-4 w-4" />}
                        {t.fetchProviderResult}
                      </button>
                    ) : null}
                  </div>
                </div>
                <dl className="mt-3 grid gap-2 text-xs text-text-muted sm:grid-cols-3">
                  <InfoRow label={t.expires} value={formatDate(item.expires_at, language)} />
                  <InfoRow label={t.status} value={item.last_job_status ?? item.status} />
                  <InfoRow label={t.attemptId} value={item.provider_attempt_id ?? t.never} />
                </dl>
                {item.candidate_launch_url ? (
                  <a
                    href={item.candidate_launch_url}
                    target="_blank"
                    rel="noreferrer"
                    className="mt-3 inline-flex min-h-10 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring"
                  >
                    <Link2 className="h-4 w-4" />
                    {t.launchAssessment}
                  </a>
                ) : null}
                {mode === "review" && item.result ? (
                  <div className="mt-4 rounded-lg border border-border bg-bg-elevated p-4">
                    <dl className="grid gap-2 text-sm sm:grid-cols-2">
                      <InfoRow
                        label={t.providerScore}
                        value={item.result.score == null ? "—" : `${item.result.score}%`}
                      />
                      <InfoRow
                        label={t.recommendation}
                        value={item.result.recommendation ?? "—"}
                      />
                    </dl>
                    <h4 className="mt-4 text-sm font-semibold">{t.evidence}</h4>
                    <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-bg p-3 text-xs leading-5">
                      {JSON.stringify(item.result.evidence, null, 2)}
                    </pre>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function providerConnectionLabel(connection: ProviderConnection | undefined): string {
  if (!connection) return "Assessment provider";
  const configured = connection.configuration.provider_label;
  return typeof configured === "string" && configured.trim()
    ? configured
    : connection.name;
}

function ResultsPanel({
  applications,
  applicationsHaveMore,
  applicationsLoading,
  onLoadMoreApplications,
  candidates,
  positions,
  language,
  searchParams,
}: {
  applications: Application[];
  applicationsHaveMore: boolean;
  applicationsLoading: boolean;
  onLoadMoreApplications: () => void | Promise<void>;
  candidates: CandidateSummary[];
  positions: Position[];
  language: "en" | "ru";
  searchParams: URLSearchParams;
}) {
  const t = COPY[language];
  const router = useRouter();
  const candidateMap = useMemo(
    () => new Map(candidates.map((item) => [item.id, item])),
    [candidates],
  );
  const positionMap = useMemo(
    () => new Map(positions.map((item) => [item.id, item])),
    [positions],
  );
  const [applicationId, setApplicationId] = useState(
    searchParams.get("application") ?? applications[0]?.id ?? "",
  );
  const [invitations, setInvitations] = useState<AssessmentInvitation[]>([]);
  const [invitationId, setInvitationId] = useState(
    searchParams.get("invitation") ?? "",
  );
  const [attempts, setAttempts] = useState<AssessmentAttemptSummary[]>([]);
  const [selectedResult, setSelectedResult] = useState<AssessmentSubmission | null>(null);
  const [loadingInvitations, setLoadingInvitations] = useState(false);
  const [loadingAttempts, setLoadingAttempts] = useState(false);
  const [approvingRetake, setApprovingRetake] = useState(false);
  const [detailPending, startDetailTransition] = useTransition();
  const invitationRequestGeneration = useRef(0);
  const attemptRequestGeneration = useRef(0);
  const detailRequestGeneration = useRef(0);
  const detailedResultRef = useRef<HTMLDivElement>(null);
  const requestedInvitationId = searchParams.get("invitation");
  const selectedInvitation = invitations.find((item) => item.id === invitationId);
  const latestAttempt = attempts.at(-1);
  const canApproveRetake = Boolean(
    selectedInvitation?.status === "active" &&
      latestAttempt?.status === "submitted" &&
      attempts.length < (selectedInvitation?.max_attempts ?? 0) &&
      (selectedInvitation?.authorized_attempts ?? 0) <= attempts.length,
  );

  useEffect(() => {
    if (selectedResult) detailedResultRef.current?.focus();
  }, [selectedResult]);

  useEffect(() => {
    const generation = ++invitationRequestGeneration.current;
    if (!applicationId) {
      setInvitations([]);
      setInvitationId("");
      setLoadingInvitations(false);
      return;
    }
    setLoadingInvitations(true);
    void listAssessmentInvitationsAction(applicationId).then((response) => {
      if (generation !== invitationRequestGeneration.current) return;
      if (response.ok) {
        setInvitations(response.data);
        const nextInvitation =
          response.data.find((item) => item.id === requestedInvitationId)?.id ??
          response.data.find((item) => item.status === "completed")?.id ??
          response.data[0]?.id ??
          "";
        setInvitationId(nextInvitation);
      } else {
        toast.error(response.error);
        setInvitations([]);
      }
      setLoadingInvitations(false);
    });
  }, [applicationId, requestedInvitationId]);

  useEffect(() => {
    const generation = ++attemptRequestGeneration.current;
    detailRequestGeneration.current += 1;
    if (!invitationId) {
      setAttempts([]);
      setSelectedResult(null);
      setLoadingAttempts(false);
      return;
    }
    setLoadingAttempts(true);
    setSelectedResult(null);
    void listAssessmentInvitationAttemptsAction(invitationId).then((response) => {
      if (generation !== attemptRequestGeneration.current) return;
      if (response.ok) setAttempts(response.data);
      else {
        toast.error(response.error);
        setAttempts([]);
      }
      setLoadingAttempts(false);
    });
  }, [invitationId]);

  function updateDeepLink(nextApplication: string, nextInvitation: string | null) {
    router.replace(
      queryHref("results", searchParams, {
        application: nextApplication || null,
        invitation: nextInvitation,
      }),
      { scroll: false },
    );
  }

  function openDetailedResult(attemptId: string) {
    const generation = ++detailRequestGeneration.current;
    startDetailTransition(async () => {
      const response = await getAssessmentResultAction(attemptId);
      if (generation !== detailRequestGeneration.current) return;
      if (!response.ok) {
        toast.error(response.error);
        return;
      }
      setSelectedResult(response.data);
    });
  }

  async function approveRetake() {
    if (!invitationId || !canApproveRetake) return;
    setApprovingRetake(true);
    const response = await approveAssessmentRetakeAction(invitationId);
    if (response.ok) {
      setInvitations((current) =>
        current.map((item) => (item.id === invitationId ? response.data : item)),
      );
      toast.success(t.retakeApproved);
    } else {
      toast.error(response.error);
    }
    setApprovingRetake(false);
  }

  return (
    <section className="rounded-xl border border-border bg-bg-elevated p-5 sm:p-6">
      <h2 className="text-lg font-semibold">{t.resultsTitle}</h2>
      <p className="mt-2 max-w-3xl text-sm leading-6 text-text-muted">
        {t.resultsHint}
      </p>
      <div className="mt-5 grid gap-4 sm:grid-cols-2">
        <FieldLabel label={t.application} htmlFor="assessment-result-application">
          <select
            id="assessment-result-application"
            value={applicationId}
            onChange={(event) => {
              const next = event.target.value;
              setApplicationId(next);
              setInvitationId("");
              updateDeepLink(next, null);
            }}
            className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30"
          >
            <option value="">{t.chooseApplication}</option>
            {applications.map((application) => (
              <option key={application.id} value={application.id}>
                {applicationLabel(application, candidateMap, positionMap)}
              </option>
            ))}
          </select>
        </FieldLabel>
        <ApplicationLoadMore
          hasMore={applicationsHaveMore}
          loading={applicationsLoading}
          onLoadMore={onLoadMoreApplications}
          language={language}
        />
        <FieldLabel label={t.invitationHistory} htmlFor="assessment-result-invitation">
          <select
            id="assessment-result-invitation"
            value={invitationId}
            disabled={!applicationId || loadingInvitations}
            onChange={(event) => {
              setInvitationId(event.target.value);
              updateDeepLink(applicationId, event.target.value || null);
            }}
            className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30 disabled:opacity-50"
          >
            <option value="">{loadingInvitations ? t.loading : t.chooseInvitation}</option>
            {invitations.map((invitation) => (
              <option key={invitation.id} value={invitation.id}>
                {invitation.recipient_email || `${invitation.token_prefix}…`} · {invitation.status} · {formatDate(invitation.created_at, language)}
              </option>
            ))}
          </select>
        </FieldLabel>
      </div>

      <div className="mt-6 border-t border-border pt-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3 className="font-semibold">{t.attemptsTitle}</h3>
          {canApproveRetake ? (
            <button
              type="button"
              onClick={() => void approveRetake()}
              disabled={approvingRetake}
              className="flex min-h-10 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
            >
              {approvingRetake ? (
                <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
              ) : (
                <RotateCcw className="h-4 w-4" />
              )}
              {approvingRetake ? t.approvingRetake : t.approveRetake}
            </button>
          ) : null}
        </div>
        {loadingAttempts ? (
          <p role="status" className="mt-3 flex items-center gap-2 text-sm text-text-muted">
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> {t.loading}
          </p>
        ) : attempts.length === 0 ? (
          <p className="mt-3 text-sm text-text-muted">{t.noAttempts}</p>
        ) : (
          <ul className="mt-3 space-y-3">
            {attempts.map((attempt) => (
              <li key={attempt.id} className="rounded-xl border border-border bg-bg p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-text-muted">
                        #{attempt.sequence} · {attempt.id.slice(0, 8)}
                      </span>
                      <StatusPill status={attempt.status} language={language} />
                    </div>
                    <p className="mt-2 text-sm text-text-muted">
                      {formatDate(attempt.started_at, language)}
                    </p>
                  </div>
                  {attempt.result ? (
                    <div className="text-right">
                      <p className="font-mono text-xl font-semibold tabular-nums">
                        {Number(attempt.result.percentage).toFixed(1)}%
                      </p>
                      <StatusPill
                        status={
                          attempt.result.needs_manual_review
                            ? "manual_review"
                            : attempt.result.passed
                              ? "passed"
                              : "failed"
                        }
                        language={language}
                      />
                    </div>
                  ) : null}
                </div>
                {(attempt.files ?? []).length > 0 ? (
                  <ul className="mt-4 space-y-2 border-t border-border pt-3">
                    {(attempt.files ?? []).map((file) => (
                      <li key={file.file_id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                        <span className="min-w-0 truncate">
                          {file.filename} · {Math.ceil(file.size_bytes / 1024)} KB
                        </span>
                        <a
                          href={`/api/assessment/reviewer/files/${encodeURIComponent(file.file_id)}`}
                          className="min-h-9 rounded-lg border border-border px-3 py-2 font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring"
                        >
                          {t.downloadFile}
                        </a>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {attempt.result ? (
                  <button
                    type="button"
                    onClick={() => openDetailedResult(attempt.id)}
                    disabled={detailPending}
                    className="mt-4 flex min-h-9 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
                  >
                    {detailPending ? (
                      <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
                    ) : (
                      <FileCheck2 className="h-4 w-4" />
                    )}
                    {t.openResult}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>
      {selectedResult ? (
        <div
          ref={detailedResultRef}
          id="assessment-detailed-result"
          tabIndex={-1}
          className="outline-none focus-visible:ring-3 focus-visible:ring-ring"
        >
          <AdminResult
            key={`${selectedResult.result.id}:${selectedResult.result.graded_at}`}
            result={selectedResult}
            language={language}
            detailPending={detailPending}
            onReload={() => openDetailedResult(selectedResult.attempt.id)}
            onGraded={(updated) => {
              setSelectedResult(updated);
              setAttempts((current) =>
                current.map((attempt) =>
                  attempt.id === updated.attempt.id && attempt.result
                    ? {
                        ...attempt,
                        result: {
                          ...attempt.result,
                          raw_score: updated.result.raw_score,
                          max_score: updated.result.max_score,
                          percentage: updated.result.percentage,
                          passed: updated.result.passed,
                          knocked_out: updated.result.knocked_out,
                          knockout_question_id: updated.result.knockout_question_id,
                          knockout_reason: updated.result.knockout_reason,
                          needs_manual_review: updated.result.needs_manual_review,
                          graded_at: updated.result.graded_at,
                        },
                      }
                    : attempt,
                ),
              );
            }}
          />
        </div>
      ) : null}
      <div className="mt-6">
        <ProviderAssessmentPanel
          mode="review"
          applicationId={applicationId}
          language={language}
        />
      </div>
    </section>
  );
}

/** @ui-only Validated manual-grading row projected from opaque evidence. */
type ManualBreakdownItem = {
  questionId: string;
  key: string;
  maxScore: number;
  score: string;
  evidenceComment: string;
};

/** @ui-only Client-side manual-grade validation errors. */
type ManualGradeFieldErrors = Record<
  string,
  { score?: boolean; evidence?: boolean }
>;

/** @ui-only Local adjudication state parsed from an additive API field before client regeneration. */
type KnockoutReviewOutcome = "confirmed" | "cleared";

/** @ui-only Validated knockout-review presentation state, never used as a transport DTO. */
type KnockoutReviewView = {
  outcome: KnockoutReviewOutcome;
  reason: string;
  reviewedByUserId: string;
  reviewedAt: string;
};

function recordValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function knockoutReviewValue(
  submission: AssessmentSubmission,
): KnockoutReviewView | null {
  const result = recordValue(submission.result);
  const review = recordValue(result?.knockout_review);
  if (
    (review?.outcome !== "confirmed" && review?.outcome !== "cleared") ||
    typeof review.reason !== "string" ||
    typeof review.reviewed_by_user_id !== "string" ||
    typeof review.reviewed_at !== "string"
  ) {
    return null;
  }
  return {
    outcome: review.outcome,
    reason: review.reason,
    reviewedByUserId: review.reviewed_by_user_id,
    reviewedAt: review.reviewed_at,
  };
}

function manualBreakdown(result: AssessmentSubmission): ManualBreakdownItem[] {
  return result.result.breakdown.flatMap((item, index) => {
    const manualGrade = recordValue(item.manual_grade);
    if (item.needs_manual_review !== true && manualGrade === null) return [];
    const questionId = typeof item.question_id === "string" ? item.question_id : "";
    const maximum = Number(item.max_score);
    if (!questionId || !Number.isFinite(maximum) || maximum < 0) return [];
    const priorScore = manualGrade?.score ?? item.score ?? 0;
    return [
      {
        questionId,
        key:
          typeof item.key === "string" && item.key.trim()
            ? item.key
            : `#${index + 1}`,
        maxScore: maximum,
        score: String(priorScore),
        evidenceComment:
          typeof manualGrade?.evidence_comment === "string"
            ? manualGrade.evidence_comment
            : "",
      },
    ];
  });
}

function ManualAnswer({
  value,
  emptyLabel,
  downloadLabel,
}: {
  value: unknown;
  emptyLabel: string;
  downloadLabel: string;
}) {
  if (value === undefined || value === null || value === "") {
    return <p className="mt-1 text-sm text-text-muted">{emptyLabel}</p>;
  }
  const file = recordValue(value);
  if (
    file &&
    typeof file.file_id === "string" &&
    typeof file.filename === "string"
  ) {
    return (
      <div className="mt-1 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-bg-elevated p-3 text-sm">
        <span className="min-w-0 break-all">
          {file.filename}
          {typeof file.size_bytes === "number"
            ? ` · ${Math.ceil(file.size_bytes / 1024)} KB`
            : ""}
        </span>
        <a
          href={`/api/assessment/reviewer/files/${encodeURIComponent(file.file_id)}`}
          className="flex min-h-10 shrink-0 items-center rounded-lg border border-border px-3 py-2 font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring"
        >
          <Link2 className="mr-2 h-4 w-4" aria-hidden />
          {downloadLabel}
        </a>
      </div>
    );
  }
  let display: string;
  if (typeof value === "string") display = value;
  else if (typeof value === "boolean" || typeof value === "number") {
    display = String(value);
  } else {
    try {
      display = JSON.stringify(value, null, 2);
    } catch {
      display = emptyLabel;
    }
  }
  return (
    <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-bg-elevated p-3 font-sans text-sm leading-6 text-text">
      {display}
    </pre>
  );
}

function AdminResult({
  result,
  language,
  detailPending,
  onReload,
  onGraded,
}: {
  result: AssessmentSubmission;
  language: "en" | "ru";
  detailPending: boolean;
  onReload: () => void;
  onGraded: (result: AssessmentSubmission) => void;
}) {
  const t = COPY[language];
  const manualItems = manualBreakdown(result);
  const [gradeValues, setGradeValues] = useState<Record<string, { score: string; evidence: string }>>(
    () =>
      Object.fromEntries(
        manualItems.map((item) => [
          item.questionId,
          { score: item.score, evidence: item.evidenceComment },
        ]),
      ),
  );
  const [gradeErrors, setGradeErrors] = useState<ManualGradeFieldErrors>({});
  const [gradeError, setGradeError] = useState<string | null>(null);
  const [gradeErrorCanReload, setGradeErrorCanReload] = useState(false);
  const [gradePending, startGradeTransition] = useTransition();
  const priorKnockoutReview = knockoutReviewValue(result);
  const [knockoutOutcome, setKnockoutOutcome] = useState<
    KnockoutReviewOutcome | ""
  >(() => priorKnockoutReview?.outcome ?? "");
  const [knockoutReason, setKnockoutReason] = useState(
    () => priorKnockoutReview?.reason ?? "",
  );
  const [knockoutErrors, setKnockoutErrors] = useState({
    outcome: false,
    reason: false,
  });
  const answersByQuestion = new Map(
    (result.attempt.answers ?? []).map((answer) => [answer.question_id, answer.value]),
  );

  function saveAssessmentReview() {
    const invalid: ManualGradeFieldErrors = {};
    const grades = manualItems.map((item) => {
      const draft = gradeValues[item.questionId] ?? { score: "", evidence: "" };
      const score = Number(draft.score);
      const scoreInvalid =
        draft.score.trim() === "" ||
        !Number.isFinite(score) ||
        score < 0 ||
        score > item.maxScore;
      const evidenceInvalid =
        !draft.evidence.trim() || draft.evidence.trim().length > 4_000;
      if (scoreInvalid || evidenceInvalid) {
        invalid[item.questionId] = {
          score: scoreInvalid,
          evidence: evidenceInvalid,
        };
      }
      return {
        question_id: item.questionId,
        score,
        evidence_comment: draft.evidence.trim(),
      };
    });
    setGradeErrors(invalid);
    setGradeError(null);
    setGradeErrorCanReload(false);
    const knockoutOutcomeInvalid = result.result.knocked_out && !knockoutOutcome;
    const knockoutReasonInvalid =
      result.result.knocked_out &&
      (!knockoutReason.trim() || knockoutReason.trim().length > 4_000);
    setKnockoutErrors({
      outcome: knockoutOutcomeInvalid,
      reason: knockoutReasonInvalid,
    });
    const firstInvalid = Object.entries(invalid)[0];
    if (firstInvalid) {
      setGradeError(t.manualGradeInvalid);
      const [questionId, fields] = firstInvalid;
      document
        .getElementById(
          `assessment-manual-${fields.score ? "score" : "evidence"}-${questionId}`,
        )
        ?.focus();
      return;
    }
    if (knockoutOutcomeInvalid || knockoutReasonInvalid) {
      setGradeError(t.knockoutReviewInvalid);
      document
        .getElementById(
          knockoutOutcomeInvalid
            ? "assessment-knockout-confirmed"
            : "assessment-knockout-reason",
        )
        ?.focus();
      return;
    }

    startGradeTransition(async () => {
      const response = await gradeAssessmentAttemptAction(result.attempt.id, {
        expected_graded_at: result.result.graded_at,
        grades,
        knockout_review:
          result.result.knocked_out && knockoutOutcome
            ? {
                outcome: knockoutOutcome,
                reason: knockoutReason.trim(),
              }
            : undefined,
      });
      if (!response.ok) {
        setGradeError(response.error);
        setGradeErrorCanReload(true);
        return;
      }
      toast.success(t.reviewSaved);
      onGraded(response.data);
    });
  }

  const knockoutReviewLabel = !result.result.knocked_out
    ? "—"
    : priorKnockoutReview?.outcome === "confirmed"
      ? t.knockoutReviewConfirmed
      : priorKnockoutReview?.outcome === "cleared"
        ? t.knockoutReviewCleared
        : t.knockoutReviewPending;

  return (
    <div className="mt-6 grid gap-5 lg:grid-cols-2">
      <section className="rounded-xl border border-border bg-bg p-5">
        <p className="text-xs font-medium uppercase tracking-wide text-text-muted">{t.score}</p>
        <div className="mt-2 flex flex-wrap items-end justify-between gap-3">
          <p className="font-mono text-4xl font-semibold tabular-nums">{Number(result.result.percentage).toFixed(1)}%</p>
          <StatusPill
            status={
              result.result.needs_manual_review
                ? "manual_review"
                : result.result.passed
                  ? "passed"
                  : "failed"
            }
            language={language}
          />
        </div>
        <dl className="mt-5 space-y-2 text-sm">
          <InfoRow label={t.score} value={`${result.result.raw_score} / ${result.result.max_score}`} />
          <InfoRow label={t.manualReview} value={result.result.needs_manual_review ? "✓" : "—"} />
          <InfoRow label={t.knockout} value={result.result.knocked_out ? result.result.knockout_reason || "✓" : "—"} />
          <InfoRow label={t.knockoutReviewTitle} value={knockoutReviewLabel} />
        </dl>
      </section>
      <section className="rounded-xl border border-border bg-bg p-5">
        <h3 className="font-semibold">{t.questionBreakdown}</h3>
        <ul className="mt-3 max-h-80 space-y-2 overflow-y-auto">
          {result.result.breakdown.map((item, index) => (
            <li key={String(item.question_id ?? index)} className="rounded-lg border border-border p-3 text-sm">
              <span className="font-mono text-xs text-text-muted">{String(item.key ?? item.question_id ?? index + 1)}</span>
              <span className="float-right font-mono tabular-nums">
                {item.visible === false ? "—" : `${String(item.score ?? 0)} / ${String(item.max_score ?? 0)}`}
              </span>
            </li>
          ))}
        </ul>
      </section>
      {manualItems.length > 0 || result.result.knocked_out ? (
        <section className="rounded-xl border border-border bg-bg p-5 lg:col-span-2">
          {manualItems.length > 0 ? (
            <>
              <h3 className="font-semibold">{t.manualGrading}</h3>
              <p className="mt-2 max-w-3xl text-sm leading-6 text-text-muted">
                {t.manualGradingHint}
              </p>
            </>
          ) : null}
          {result.result.knocked_out ? (
            <p className="mt-2 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-text">
              {t.knockoutSeparate}
            </p>
          ) : null}
          {manualItems.length > 0 ? <div className="mt-5 space-y-4">
            {manualItems.map((item) => {
              const draft = gradeValues[item.questionId] ?? { score: "", evidence: "" };
              const invalid = gradeErrors[item.questionId];
              const errorId = `assessment-manual-error-${item.questionId}`;
              return (
                <fieldset
                  key={item.questionId}
                  className="rounded-xl border border-border p-4"
                >
                  <legend className="px-1 font-mono text-sm font-semibold">
                    {item.key}
                  </legend>
                  <div className="mb-4">
                    <p className="text-xs font-medium text-text-muted">
                      {t.candidateAnswer}
                    </p>
                    <ManualAnswer
                      value={answersByQuestion.get(item.questionId)}
                      emptyLabel={t.noCandidateAnswer}
                      downloadLabel={t.downloadFile}
                    />
                  </div>
                  <div className="grid gap-4 sm:grid-cols-[minmax(10rem,0.35fr)_minmax(0,1fr)]">
                    <FieldLabel
                      label={`${t.manualScore} (0–${item.maxScore})`}
                      htmlFor={`assessment-manual-score-${item.questionId}`}
                    >
                      <input
                        id={`assessment-manual-score-${item.questionId}`}
                        type="number"
                        inputMode="decimal"
                        min={0}
                        max={item.maxScore}
                        step="0.0001"
                        required
                        aria-invalid={invalid?.score || undefined}
                        aria-describedby={invalid?.score ? errorId : undefined}
                        value={draft.score}
                        onChange={(event) => {
                          const score = event.target.value;
                          setGradeValues((current) => ({
                            ...current,
                            [item.questionId]: { ...draft, score },
                          }));
                          setGradeErrors((current) => ({
                            ...current,
                            [item.questionId]: {
                              ...current[item.questionId],
                              score: false,
                            },
                          }));
                        }}
                        className="min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30 aria-invalid:border-destructive"
                      />
                    </FieldLabel>
                    <FieldLabel
                      label={t.evidenceComment}
                      htmlFor={`assessment-manual-evidence-${item.questionId}`}
                    >
                      <textarea
                        id={`assessment-manual-evidence-${item.questionId}`}
                        rows={3}
                        required
                        maxLength={4_000}
                        aria-invalid={invalid?.evidence || undefined}
                        aria-describedby={invalid?.evidence ? errorId : undefined}
                        placeholder={t.evidencePlaceholder}
                        value={draft.evidence}
                        onChange={(event) => {
                          const evidence = event.target.value;
                          setGradeValues((current) => ({
                            ...current,
                            [item.questionId]: { ...draft, evidence },
                          }));
                          setGradeErrors((current) => ({
                            ...current,
                            [item.questionId]: {
                              ...current[item.questionId],
                              evidence: false,
                            },
                          }));
                        }}
                        className="w-full resize-y rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30 aria-invalid:border-destructive"
                      />
                    </FieldLabel>
                  </div>
                  {invalid?.score || invalid?.evidence ? (
                    <p id={errorId} className="mt-2 text-sm text-destructive">
                      {t.manualGradeInvalid}
                    </p>
                  ) : null}
                </fieldset>
              );
            })}
          </div> : null}
          {result.result.knocked_out ? (
            <fieldset
              className="mt-5 rounded-xl border border-warning/40 bg-warning/5 p-4"
              aria-describedby="assessment-knockout-review-hint"
            >
              <legend className="px-1 text-sm font-semibold">
                {t.knockoutReviewTitle}
              </legend>
              <p
                id="assessment-knockout-review-hint"
                className="max-w-3xl text-sm leading-6 text-text-muted"
              >
                {t.knockoutReviewHint}
              </p>
              <dl className="mt-4 rounded-lg border border-border bg-bg p-3 text-sm">
                <InfoRow
                  label={t.knockoutMatchEvidence}
                  value={result.result.knockout_reason || "—"}
                />
              </dl>
              <fieldset className="mt-4">
                <legend className="text-xs font-medium text-text-muted">
                  {t.knockoutOutcome}
                </legend>
                <div className="mt-2 grid gap-2 md:grid-cols-2">
                  {(
                    [
                      ["confirmed", t.knockoutConfirmed],
                      ["cleared", t.knockoutCleared],
                    ] as const
                  ).map(([outcome, label]) => (
                    <label
                      key={outcome}
                      className="flex min-h-11 cursor-pointer items-start gap-3 rounded-lg border border-border bg-bg p-3 text-sm focus-within:ring-3 focus-within:ring-ring"
                    >
                      <input
                        id={`assessment-knockout-${outcome}`}
                        type="radio"
                        name={`assessment-knockout-outcome-${result.result.id}`}
                        value={outcome}
                        checked={knockoutOutcome === outcome}
                        aria-invalid={knockoutErrors.outcome || undefined}
                        aria-describedby={
                          knockoutErrors.outcome
                            ? "assessment-knockout-review-error"
                            : "assessment-knockout-review-hint"
                        }
                        onChange={() => {
                          setKnockoutOutcome(outcome);
                          setKnockoutErrors((current) => ({
                            ...current,
                            outcome: false,
                          }));
                        }}
                        className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
                      />
                      <span>{label}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="mt-4">
                <label
                  htmlFor="assessment-knockout-reason"
                  className="block text-xs font-medium text-text-muted"
                >
                  {t.knockoutReviewReason}
                </label>
                <textarea
                  id="assessment-knockout-reason"
                  rows={4}
                  required
                  maxLength={4_000}
                  value={knockoutReason}
                  aria-invalid={knockoutErrors.reason || undefined}
                  aria-describedby={
                    knockoutErrors.reason
                      ? "assessment-knockout-review-error"
                      : "assessment-knockout-review-hint"
                  }
                  placeholder={t.knockoutReviewPlaceholder}
                  onChange={(event) => {
                    setKnockoutReason(event.target.value);
                    setKnockoutErrors((current) => ({
                      ...current,
                      reason: false,
                    }));
                  }}
                  className="mt-1 w-full resize-y rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30 aria-invalid:border-destructive"
                />
              </div>
              {knockoutErrors.outcome || knockoutErrors.reason ? (
                <p
                  id="assessment-knockout-review-error"
                  className="mt-2 text-sm text-destructive"
                >
                  {t.knockoutReviewInvalid}
                </p>
              ) : null}
            </fieldset>
          ) : null}
          <div className="mt-5 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={saveAssessmentReview}
              disabled={gradePending || detailPending}
              className="flex min-h-10 items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:bg-primary-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
            >
              {gradePending ? (
                <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
              ) : (
                <CheckCircle2 className="h-4 w-4" />
              )}
              {gradePending ? t.savingReview : t.saveReview}
            </button>
            {gradeErrorCanReload ? (
              <button
                type="button"
                onClick={onReload}
                disabled={detailPending || gradePending}
                className="flex min-h-10 items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
              >
                <RefreshCw className={cn("h-4 w-4", detailPending && "animate-spin motion-reduce:animate-none")} />
                {t.reloadResult}
              </button>
            ) : null}
          </div>
          <div aria-live="polite" className="mt-3 min-h-5 text-sm">
            {gradeError ? (
              <p role="alert" className="text-destructive">
                {gradeError} {gradeErrorCanReload ? t.manualGradeReload : ""}
              </p>
            ) : gradePending ? (
              <p role="status" className="text-text-muted">{t.savingReview}</p>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function VersionSummary({ version, language }: { version: AssessmentVersion; language: "en" | "ru" }) {
  const t = COPY[language];
  const count = version.sections.reduce((sum, section) => sum + section.questions.length, 0);
  return (
    <li className="rounded-lg border border-border bg-bg p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium">v{version.version} · {version.title}</p>
          <p className="mt-1 text-xs text-text-muted">{count} {t.questions} · {version.passing_percentage}%</p>
        </div>
        <time className="text-xs text-text-muted" dateTime={version.published_at}>
          {formatDate(version.published_at, language)}
        </time>
      </div>
    </li>
  );
}

function StatusPill({ status, language }: { status: string; language: "en" | "ru" }) {
  const t = COPY[language];
  const labels: Record<string, string> = {
    active: t.active,
    archived: t.archived,
    completed: language === "ru" ? "Завершено" : "Completed",
    expired: language === "ru" ? "Истекло" : "Expired",
    revoked: language === "ru" ? "Отозвано" : "Revoked",
    submitted: language === "ru" ? "Отправлено" : "Submitted",
    in_progress: language === "ru" ? "В процессе" : "In progress",
    manual_review: t.manualReview,
    passed: t.passed,
    failed: t.notPassed,
  };
  return (
    <span className={cn(
      "inline-flex rounded-full px-2 py-0.5 text-[11px] font-semibold",
      status === "active" || status === "passed" || status === "completed" || status === "submitted"
        ? "bg-success/15 text-success"
        : status === "in_progress" || status === "manual_review"
          ? "bg-warning/15 text-warning"
        : status === "expired" || status === "archived"
          ? "bg-bg-active text-text-muted"
          : "bg-destructive/10 text-destructive",
    )}>
      {labels[status] ?? status}
    </span>
  );
}

function FieldLabel({ label, htmlFor, children }: { label: string; htmlFor: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="block text-xs font-medium text-text-muted">{label}</label>
      <div className="mt-1">{children}</div>
    </div>
  );
}

function ApplicationLoadMore({
  hasMore,
  loading,
  onLoadMore,
  language,
}: {
  hasMore: boolean;
  loading: boolean;
  onLoadMore: () => void | Promise<void>;
  language: "en" | "ru";
}) {
  if (!hasMore) return null;
  const t = COPY[language];
  return (
    <button
      type="button"
      onClick={() => void onLoadMore()}
      disabled={loading}
      className="flex min-h-10 items-center justify-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
    >
      {loading ? (
        <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
      ) : (
        <Plus className="h-4 w-4" />
      )}
      {loading ? t.loadingApplications : t.loadMoreApplications}
    </button>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <dt className="text-text-muted">{label}</dt>
      <dd className="text-right text-text">{value}</dd>
    </div>
  );
}

function applicationLabel(
  application: Application,
  candidates: Map<string, CandidateSummary>,
  positions: Map<string, Position>,
) {
  const candidate = candidates.get(application.candidate_id);
  const position = positions.get(application.position_id);
  const candidateLabel = candidate?.source_ref || candRef(application.candidate_id);
  const positionLabel = position?.title || prjRef(application.position_id);
  return `${candidateLabel} → ${positionLabel} · ${application.stage}`;
}

function formatDate(value: string, language: "en" | "ru") {
  return new Intl.DateTimeFormat(language === "ru" ? "ru-RU" : "en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
