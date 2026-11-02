"use client";

import Link from "next/link";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Clock3,
  FileUp,
  Loader2,
  RotateCcw,
  Save,
  Send,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";

import type { components } from "@/lib/generated/api";
import {
  answerIsPresent,
  visibleQuestions,
  type AssessmentAnswers,
} from "@/lib/assessment-conditions";
import { cn } from "@/lib/utils";

type PublicAssessmentLaunch = components["schemas"]["PublicAssessmentLaunch"];
type PublicAssessmentLaunchContract = PublicAssessmentLaunch;
type PublicQuestion = components["schemas"]["PublicAssessmentQuestion"];
type AssessmentAttempt = components["schemas"]["AssessmentAttemptOut"];
type PublicAssessmentResult = components["schemas"]["PublicAssessmentResultOut"];
type AssessmentFileUpload = components["schemas"]["AssessmentFileUploadOut"];
type PublicAssessmentSubmission = {
  attempt: AssessmentAttempt;
  result: PublicAssessmentResult & { needs_manual_review: boolean };
};

/** @ui-only Client autosave state machine. */
type SaveState = "idle" | "waiting" | "saving" | "saved" | "error";

const COPY = {
  en: {
    brand: "Recruiter assessment",
    language: "Language",
    invitationExpiry: "Invitation expires",
    timeLimit: "Time limit",
    minutes: "minutes",
    untimed: "No time limit",
    accommodation: "Approved accommodation",
    extraTime: (count: number) => `${count} extra minutes`,
    breaksAllowed: "You may take breaks; the timer continues",
    screenReaderOptimized: "Your screen-reader support preference was recorded",
    introTitle: "Before you start",
    introBody:
      "Your progress is saved automatically after you begin. Use the same private link to resume on this device.",
    start: "Start assessment",
    resume: "Resume assessment",
    startAnother: "Start another attempt",
    attemptsRemaining: (count: number) =>
      `${count} ${count === 1 ? "attempt" : "attempts"} remaining`,
    starting: "Starting…",
    questionProgress: (answered: number, total: number) => `${answered} of ${total} answered`,
    required: "Required",
    optional: "Optional",
    clearAnswer: "Clear answer",
    yes: "Yes",
    no: "No",
    selectOne: "Select one answer",
    selectMany: "Select all that apply",
    number: "Enter a number",
    short: "Type your answer",
    long: "Type your detailed answer",
    url: "https://example.com",
    file: "Choose file",
    fileUploading: "Uploading file…",
    fileUploaded: "File uploaded",
    removeFile: "Remove file",
    saved: "All changes saved",
    saving: "Saving…",
    waiting: "Changes waiting to save",
    saveFailed: "Progress was not saved",
    retry: "Retry save",
    lastSaved: "Last saved",
    review: "Review and submit",
    reviewHint: "Submitting is final. You will not be able to edit this attempt.",
    submit: "Submit final answers",
    cancel: "Keep editing",
    submitting: "Submitting…",
    missing: "Answer this required question before submitting.",
    missingSummary: (count: number) =>
      `${count} required ${count === 1 ? "answer is" : "answers are"} missing.`,
    expiredTitle: "Time is up",
    expiredBody: "This attempt has expired and can no longer be edited.",
    submittedTitle: "Assessment submitted",
    submittedBody: "Your answers were received. You may close this page.",
    score: "Score",
    passed: "Passed",
    notPassed: "Not passed",
    manualReview: "Manual review required",
    knockout: "A required condition was not met",
    alreadySubmitted:
      "This attempt was already submitted. The hiring team has access to your result.",
    remaining: "Time remaining",
    expired: "Expired",
    serviceError: "Something went wrong. Check your connection and try again.",
  },
  ru: {
    brand: "Оценивание Recruiter",
    language: "Язык",
    invitationExpiry: "Приглашение действует до",
    timeLimit: "Лимит времени",
    minutes: "мин.",
    untimed: "Без ограничения времени",
    accommodation: "Согласованная адаптация",
    extraTime: (count: number) => `Дополнительное время: ${count} мин.`,
    breaksAllowed: "Можно делать перерывы; таймер продолжает идти",
    screenReaderOptimized: "Запрос на поддержку программы чтения с экрана зафиксирован",
    introTitle: "Перед началом",
    introBody:
      "После начала прогресс сохраняется автоматически. Чтобы продолжить на этом устройстве, откройте ту же персональную ссылку.",
    start: "Начать оценивание",
    resume: "Продолжить оценивание",
    startAnother: "Начать ещё одну попытку",
    attemptsRemaining: (count: number) => `Осталось попыток: ${count}`,
    starting: "Запуск…",
    questionProgress: (answered: number, total: number) => `Отвечено ${answered} из ${total}`,
    required: "Обязательно",
    optional: "Необязательно",
    clearAnswer: "Очистить ответ",
    yes: "Да",
    no: "Нет",
    selectOne: "Выберите один ответ",
    selectMany: "Можно выбрать несколько ответов",
    number: "Введите число",
    short: "Введите ответ",
    long: "Введите подробный ответ",
    url: "https://example.com",
    file: "Выбрать файл",
    fileUploading: "Загрузка файла…",
    fileUploaded: "Файл загружен",
    removeFile: "Удалить файл",
    saved: "Все изменения сохранены",
    saving: "Сохранение…",
    waiting: "Изменения ожидают сохранения",
    saveFailed: "Не удалось сохранить прогресс",
    retry: "Повторить",
    lastSaved: "Сохранено",
    review: "Проверить и отправить",
    reviewHint: "Отправка окончательна. Изменить ответы в этой попытке будет нельзя.",
    submit: "Отправить ответы",
    cancel: "Вернуться к ответам",
    submitting: "Отправка…",
    missing: "Ответьте на этот обязательный вопрос.",
    missingSummary: (count: number) => `Не заполнено обязательных полей: ${count}.`,
    expiredTitle: "Время истекло",
    expiredBody: "Срок этой попытки истёк, редактирование больше недоступно.",
    submittedTitle: "Оценивание отправлено",
    submittedBody: "Ответы получены. Эту страницу можно закрыть.",
    score: "Результат",
    passed: "Пройдено",
    notPassed: "Не пройдено",
    manualReview: "Требуется проверка специалистом",
    knockout: "Обязательное условие не выполнено",
    alreadySubmitted:
      "Эта попытка уже отправлена. Результат доступен команде найма.",
    remaining: "Осталось времени",
    expired: "Истекло",
    serviceError: "Произошла ошибка. Проверьте подключение и повторите попытку.",
  },
} as const;

function initialAnswers(attempt: AssessmentAttempt | null | undefined): AssessmentAnswers {
  return Object.fromEntries(
    (attempt?.answers ?? []).map((answer) => [answer.question_id, answer.value]),
  );
}

function optionList(question: PublicQuestion): Array<{ value: string; label: string }> {
  return question.options.flatMap((option) => {
    const value = typeof option.value === "string" ? option.value : null;
    const label = typeof option.label === "string" ? option.label : value;
    return value && label ? [{ value, label }] : [];
  });
}

function validationNumber(question: PublicQuestion, key: string): number | undefined {
  const value = question.validation?.[key];
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return undefined;
}

function humanDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

async function responseData<T>(response: Response, safeMessage: string): Promise<T> {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(safeMessage);
  }
  return payload as T;
}

export function PublicAssessment({
  launch,
  language,
}: {
  launch: PublicAssessmentLaunchContract;
  language: "en" | "ru";
}) {
  const t = COPY[language];
  const [attempt, setAttempt] = useState<AssessmentAttempt | null>(launch.attempt ?? null);
  const [answers, setAnswers] = useState<AssessmentAnswers>(() => initialAnswers(launch.attempt));
  const answersRef = useRef(answers);
  const [starting, setStarting] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [lastSaved, setLastSaved] = useState<string | null>(launch.attempt?.last_saved_at ?? null);
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [submission, setSubmission] = useState<PublicAssessmentSubmission | null>(null);
  const [existingResult, setExistingResult] =
    useState<PublicAssessmentResult | null>(launch.result ?? null);
  const [attemptsRemaining, setAttemptsRemaining] = useState(
    Number.isInteger(launch.attempts_remaining) &&
      (launch.attempts_remaining ?? -1) >= 0
      ? launch.attempts_remaining!
      : 0,
  );
  const [canStartAnother, setCanStartAnother] = useState(
    launch.can_start_attempt === true,
  );
  const [pageError, setPageError] = useState<string | null>(null);
  const [missing, setMissing] = useState<Set<string>>(new Set());
  const [now, setNow] = useState(Date.now());
  const dirtyRef = useRef<Map<string, unknown>>(new Map());
  const saveTimerRef = useRef<number | null>(null);
  const savingRef = useRef<Promise<boolean> | null>(null);

  const allQuestions = useMemo(
    () =>
      launch.sections
        .slice()
        .sort((a, b) => a.position - b.position)
        .flatMap((section) =>
          section.questions.slice().sort((a, b) => a.position - b.position),
        ),
    [launch.sections],
  );
  const visible = useMemo(
    () => visibleQuestions(allQuestions, answers),
    [allQuestions, answers],
  );
  const visibleIds = useMemo(() => new Set(visible.map((question) => question.id)), [visible]);
  const answeredCount = visible.filter((question) => answerIsPresent(answers[question.id])).length;
  const expiresAt = attempt ? new Date(attempt.expires_at).getTime() : null;
  const remainingSeconds = expiresAt == null ? null : Math.max(0, Math.ceil((expiresAt - now) / 1000));
  const isExpired = attempt?.status === "expired" || remainingSeconds === 0;
  const isSubmitted =
    attempt?.status === "submitted" || submission != null || existingResult != null;

  useEffect(() => {
    answersRef.current = answers;
  }, [answers]);

  useEffect(() => {
    if (!attempt || attempt.status !== "in_progress") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [attempt]);

  useEffect(
    () => () => {
      if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    },
    [],
  );

  const flushSave = useCallback(
    async (fullAnswers?: AssessmentAnswers): Promise<boolean> => {
      if (!attempt || attempt.status !== "in_progress") return false;
      if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;

      const entriesById = new Map<string, unknown>();
      if (fullAnswers) {
        for (const [id, value] of Object.entries(fullAnswers)) {
          entriesById.set(id, value);
        }
      }
      // Edits made while an earlier save is in flight are newer than the
      // snapshot supplied by submit. Merge them last, including explicit null
      // values, so clearing an optional answer can never resurrect stale text.
      for (const [id, value] of dirtyRef.current.entries()) {
        entriesById.set(id, value);
      }
      const entries = Array.from(entriesById.entries());
      if (entries.length === 0) {
        setSaveState("saved");
        return true;
      }
      for (const [id] of entries) dirtyRef.current.delete(id);
      setSaveState("saving");
      setSaveError(null);

      const request = responseData<AssessmentAttempt>(
        await fetch(`/api/assessment/attempt/${encodeURIComponent(attempt.id)}/answers`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            answers: entries.map(([question_id, value]) => ({ question_id, value })),
          }),
        }),
        t.serviceError,
      )
        .then((savedAttempt) => {
          setAttempt(savedAttempt);
          setLastSaved(savedAttempt.last_saved_at);
          setSaveState("saved");
          return true;
        })
        .catch((error: unknown) => {
          for (const [id, value] of entries) {
            if (!dirtyRef.current.has(id)) dirtyRef.current.set(id, value);
          }
          setSaveState("error");
          setSaveError(error instanceof Error ? error.message : t.serviceError);
          return false;
        })
        .finally(() => {
          savingRef.current = null;
        });
      savingRef.current = request;
      return request;
    },
    [attempt, t.serviceError],
  );

  function scheduleSave(questionId: string, value: unknown) {
    dirtyRef.current.set(questionId, value);
    setSaveState("waiting");
    setSaveError(null);
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => void flushSave(), 800);
  }

  function updateAnswer(questionId: string, value: unknown) {
    setAnswers((current) => ({ ...current, [questionId]: value }));
    setMissing((current) => {
      if (!current.has(questionId)) return current;
      const next = new Set(current);
      next.delete(questionId);
      return next;
    });
    scheduleSave(questionId, value);
  }

  function removeLocalAnswer(questionId: string) {
    dirtyRef.current.delete(questionId);
    setAnswers((current) => {
      const next = { ...current };
      delete next[questionId];
      return next;
    });
  }

  async function startAttempt() {
    setStarting(true);
    setPageError(null);
    try {
      const started = await responseData<AssessmentAttempt>(
        await fetch("/api/assessment/attempt", { method: "POST" }),
        t.serviceError,
      );
      setAttempt(started);
      setSubmission(null);
      setExistingResult(null);
      setCanStartAnother(false);
      setAttemptsRemaining((current) => Math.max(0, current - 1));
      const restored = initialAnswers(started);
      answersRef.current = restored;
      setAnswers(restored);
      setLastSaved(started.last_saved_at);
      setSaveState("saved");
      setSaveError(null);
      setMissing(new Set());
      setConfirming(false);
      dirtyRef.current.clear();
    } catch (error) {
      setPageError(error instanceof Error ? error.message : t.serviceError);
    } finally {
      setStarting(false);
    }
  }

  function beginSubmit() {
    const missingQuestions = visible.filter(
      (question) => question.required && !answerIsPresent(answersRef.current[question.id]),
    );
    if (missingQuestions.length > 0) {
      setMissing(new Set(missingQuestions.map((question) => question.id)));
      setPageError(t.missingSummary(missingQuestions.length));
      document.getElementById(`question-${missingQuestions[0].id}`)?.focus();
      return;
    }
    setPageError(null);
    setConfirming(true);
  }

  async function submitAttempt() {
    if (!attempt) return;
    setSubmitting(true);
    setPageError(null);
    try {
      if (savingRef.current) await savingRef.current;
      // Send a terminal scalar snapshot before submit. Explicit nulls clear
      // answers that the candidate removed or that became hidden when a
      // conditional branch changed. File answers use their dedicated
      // upload/delete lifecycle and are included only when present.
      const finalAnswers = Object.fromEntries(
        allQuestions.flatMap((question) => {
          const value = answersRef.current[question.id];
          if (question.question_type === "file") {
            return visibleIds.has(question.id) && answerIsPresent(value)
              ? [[question.id, value] as const]
              : [];
          }
          return [
            [
              question.id,
              visibleIds.has(question.id) && answerIsPresent(value)
                ? value
                : null,
            ] as const,
          ];
        }),
      );
      if (!(await flushSave(finalAnswers))) {
        throw new Error(saveError || t.saveFailed);
      }
      const completed = await responseData<PublicAssessmentSubmission>(
        await fetch(
          `/api/assessment/attempt/${encodeURIComponent(attempt.id)}/submit`,
          { method: "POST" },
        ),
        t.serviceError,
      );
      setSubmission(completed);
      setAttempt(completed.attempt);
      setCanStartAnother(attemptsRemaining > 0);
      setConfirming(false);
      window.scrollTo({
        top: 0,
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
      });
    } catch (error) {
      setPageError(error instanceof Error ? error.message : t.serviceError);
      setConfirming(false);
    } finally {
      setSubmitting(false);
    }
  }

  if (isSubmitted) {
    return (
      <PublicShell language={language} label={t.language} brand={t.brand}>
        <section className="mx-auto w-full max-w-2xl rounded-2xl border border-border bg-bg-elevated p-6 shadow-xl sm:p-10">
          <CheckCircle2 className="h-10 w-10 text-success" aria-hidden />
          <h1 className="mt-5 text-2xl font-semibold tracking-tight sm:text-3xl">
            {t.submittedTitle}
          </h1>
          <p className="mt-3 text-base leading-7 text-text-muted">
            {submission ? t.submittedBody : t.alreadySubmitted}
          </p>
          {submission ? (
            <ResultSummary submission={submission} language={language} />
          ) : existingResult ? (
            <PublicResultSummary result={existingResult} language={language} />
          ) : null}
          {canStartAnother ? (
            <RetakeButton
              language={language}
              remaining={attemptsRemaining}
              pending={starting}
              onStart={() => void startAttempt()}
            />
          ) : null}
          {pageError ? (
            <p role="alert" className="mt-4 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
              {pageError}
            </p>
          ) : null}
        </section>
      </PublicShell>
    );
  }

  if (isExpired) {
    return (
      <PublicShell language={language} label={t.language} brand={t.brand}>
        <section className="mx-auto w-full max-w-2xl rounded-2xl border border-border bg-bg-elevated p-6 shadow-xl sm:p-10">
          <Clock3 className="h-10 w-10 text-warning" aria-hidden />
          <h1 className="mt-5 text-2xl font-semibold tracking-tight sm:text-3xl">
            {t.expiredTitle}
          </h1>
          <p className="mt-3 text-base leading-7 text-text-muted">{t.expiredBody}</p>
          {canStartAnother ? (
            <RetakeButton
              language={language}
              remaining={attemptsRemaining}
              pending={starting}
              onStart={() => void startAttempt()}
            />
          ) : null}
          {pageError ? (
            <p role="alert" className="mt-4 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
              {pageError}
            </p>
          ) : null}
        </section>
      </PublicShell>
    );
  }

  if (!attempt) {
    return (
      <PublicShell language={language} label={t.language} brand={t.brand}>
        <section className="mx-auto w-full max-w-2xl rounded-2xl border border-border bg-bg-elevated p-6 shadow-xl sm:p-10">
          <p className="text-sm font-medium text-primary">{t.brand}</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">
            {launch.title}
          </h1>
          {launch.instructions ? (
            <p className="mt-4 whitespace-pre-line text-base leading-7 text-text-muted">
              {launch.instructions}
            </p>
          ) : null}
          <dl className="mt-7 grid gap-3 rounded-xl border border-border bg-bg p-4 sm:grid-cols-2">
            <Info label={t.invitationExpiry} value={formatDate(launch.expires_at, language)} />
            <Info
              label={t.timeLimit}
              value={
                launch.time_limit_minutes
                  ? `${launch.time_limit_minutes} ${t.minutes}`
                  : t.untimed
              }
            />
          </dl>
          {launch.accommodations &&
          (launch.accommodations.extra_time_minutes > 0 ||
            launch.accommodations.breaks_allowed ||
            launch.accommodations.screen_reader_optimized) ? (
            <section
              aria-labelledby="assessment-accommodation-title"
              className="mt-4 rounded-xl border border-primary/30 bg-primary/5 p-4"
            >
              <h2 id="assessment-accommodation-title" className="text-sm font-semibold">
                {t.accommodation}
              </h2>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-text-muted">
                {launch.accommodations.extra_time_minutes > 0 ? (
                  <li>{t.extraTime(launch.accommodations.extra_time_minutes)}</li>
                ) : null}
                {launch.accommodations.breaks_allowed ? <li>{t.breaksAllowed}</li> : null}
                {launch.accommodations.screen_reader_optimized ? (
                  <li>{t.screenReaderOptimized}</li>
                ) : null}
              </ul>
            </section>
          ) : null}
          <div className="mt-7 rounded-xl border border-border bg-bg p-4">
            <h2 className="font-semibold">{t.introTitle}</h2>
            <p className="mt-2 text-sm leading-6 text-text-muted">{t.introBody}</p>
          </div>
          {pageError ? (
            <p role="alert" className="mt-4 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
              {pageError}
            </p>
          ) : null}
          <button
            type="button"
            onClick={() => void startAttempt()}
            disabled={starting}
            className="mt-7 flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-primary px-5 py-3 text-base font-semibold text-primary-foreground outline-none transition-colors hover:bg-primary/85 focus-visible:ring-4 focus-visible:ring-ring/50 disabled:opacity-60"
          >
            {starting ? <Loader2 className="h-5 w-5 animate-spin motion-reduce:animate-none" /> : null}
            {starting ? t.starting : launch.attempt ? t.resume : t.start}
          </button>
        </section>
      </PublicShell>
    );
  }

  return (
    <PublicShell language={language} label={t.language} brand={t.brand}>
      <div className="mx-auto w-full max-w-3xl">
        <header className="rounded-2xl border border-border bg-bg-elevated p-5 shadow-lg sm:p-7">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-primary">{t.brand}</p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
                {launch.title}
              </h1>
            </div>
            {remainingSeconds != null ? (
              <div
                role="timer"
                aria-label={`${t.remaining}: ${humanDuration(remainingSeconds)}`}
                className={cn(
                  "flex items-center gap-2 rounded-lg border px-3 py-2 font-mono text-sm tabular-nums",
                  remainingSeconds <= 300
                    ? "border-warning/40 bg-warning/10 text-warning"
                    : "border-border bg-bg text-text",
                )}
              >
                <Clock3 className="h-4 w-4" aria-hidden />
                {humanDuration(remainingSeconds)}
              </div>
            ) : null}
          </div>
          <div className="mt-5 flex items-center gap-3">
            <progress
              aria-label={t.questionProgress(answeredCount, visible.length)}
              max={Math.max(visible.length, 1)}
              value={answeredCount}
              className="h-2 min-w-0 flex-1 overflow-hidden rounded-full accent-primary"
            />
            <span className="whitespace-nowrap text-sm text-text-muted">
              {t.questionProgress(answeredCount, visible.length)}
            </span>
          </div>
        </header>

        {pageError ? (
          <div role="alert" className="mt-5 flex gap-3 rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
            <AlertCircle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden />
            <span>{pageError}</span>
          </div>
        ) : null}

        <div className="mt-5 space-y-7">
          {launch.sections
            .slice()
            .sort((a, b) => a.position - b.position)
            .map((section) => {
              const sectionQuestions = section.questions
                .slice()
                .sort((a, b) => a.position - b.position)
                .filter((question) => visibleIds.has(question.id));
              if (sectionQuestions.length === 0) return null;
              return (
                <section key={section.id} aria-labelledby={`section-${section.id}`}>
                  <div className="mb-3 px-1">
                    <h2 id={`section-${section.id}`} className="text-lg font-semibold">
                      {section.title}
                    </h2>
                    {section.description ? (
                      <p className="mt-1 text-sm leading-6 text-text-muted">{section.description}</p>
                    ) : null}
                  </div>
                  <div className="space-y-3">
                    {sectionQuestions.map((question, index) => (
                      <QuestionField
                        key={question.id}
                        question={question}
                        number={allQuestions.indexOf(question) + 1 || index + 1}
                        value={answers[question.id]}
                        error={missing.has(question.id) ? t.missing : null}
                        language={language}
                        attemptId={attempt.id}
                        onChange={(value) => updateAnswer(question.id, value)}
                        onRemove={() => removeLocalAnswer(question.id)}
                      />
                    ))}
                  </div>
                </section>
              );
            })}
        </div>

        <section className="mt-7 rounded-2xl border border-border bg-bg-elevated p-5 sm:p-7">
          <h2 className="text-lg font-semibold">{t.review}</h2>
          <p className="mt-2 text-sm leading-6 text-text-muted">{t.reviewHint}</p>
          {confirming ? (
            <div className="mt-5 rounded-xl border border-warning/30 bg-warning/10 p-4">
              <p className="font-medium">{t.reviewHint}</p>
              <div className="mt-4 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
                <button
                  type="button"
                  onClick={() => setConfirming(false)}
                  disabled={submitting}
                  className="min-h-11 rounded-lg border border-border px-4 py-2 text-sm font-medium focus-visible:ring-4 focus-visible:ring-ring/50"
                >
                  {t.cancel}
                </button>
                <button
                  type="button"
                  onClick={() => void submitAttempt()}
                  disabled={submitting}
                  className="flex min-h-11 items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground focus-visible:ring-4 focus-visible:ring-ring/50 disabled:opacity-60"
                >
                  {submitting ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Send className="h-4 w-4" />}
                  {submitting ? t.submitting : t.submit}
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={beginSubmit}
              className="mt-5 flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-primary px-5 py-3 font-semibold text-primary-foreground focus-visible:ring-4 focus-visible:ring-ring/50 sm:w-auto"
            >
              <Check className="h-5 w-5" />
              {t.review}
            </button>
          )}
        </section>

        <div
          aria-live="polite"
          className="sticky bottom-3 z-10 mx-auto mt-5 flex min-h-11 w-fit max-w-full items-center gap-2 rounded-full border border-border bg-bg-elevated/95 px-4 py-2 text-sm text-text-muted shadow-lg backdrop-blur"
        >
          {saveState === "saving" ? (
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
          ) : saveState === "error" ? (
            <AlertCircle className="h-4 w-4 text-destructive" aria-hidden />
          ) : (
            <Save className="h-4 w-4" aria-hidden />
          )}
          <span>
            {saveState === "saving"
              ? t.saving
              : saveState === "waiting"
                ? t.waiting
                : saveState === "error"
                  ? saveError || t.saveFailed
                  : lastSaved
                    ? `${t.saved} · ${formatTime(lastSaved, language)}`
                    : t.saved}
          </span>
          {saveState === "error" ? (
            <button
              type="button"
              onClick={() => void flushSave()}
              className="flex min-h-8 items-center gap-1 rounded-md px-2 font-medium text-primary hover:bg-bg-hover focus-visible:ring-2 focus-visible:ring-ring"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              {t.retry}
            </button>
          ) : null}
        </div>
      </div>
    </PublicShell>
  );
}

function RetakeButton({
  language,
  remaining,
  pending,
  onStart,
}: {
  language: "en" | "ru";
  remaining: number;
  pending: boolean;
  onStart: () => void;
}) {
  const t = COPY[language];
  return (
    <div className="mt-7 rounded-xl border border-border bg-bg p-4">
      <p className="text-sm text-text-muted">{t.attemptsRemaining(remaining)}</p>
      <button
        type="button"
        onClick={onStart}
        disabled={pending}
        className="mt-3 flex min-h-11 w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-primary-foreground focus-visible:ring-4 focus-visible:ring-ring/50 disabled:opacity-60 sm:w-auto"
      >
        {pending ? (
          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
        ) : (
          <RotateCcw className="h-4 w-4" aria-hidden />
        )}
        {pending ? t.starting : t.startAnother}
      </button>
    </div>
  );
}

function PublicShell({
  language,
  label,
  brand,
  children,
}: {
  language: "en" | "ru";
  label: string;
  brand: string;
  children: React.ReactNode;
}) {
  return (
    <main className="min-h-dvh bg-background px-4 py-5 sm:px-6 sm:py-8">
      <div className="mx-auto mb-5 flex w-full max-w-3xl items-center justify-between gap-4">
        <span className="text-sm font-semibold tracking-tight">{brand}</span>
        <nav aria-label={label} className="flex rounded-lg border border-border bg-bg-elevated p-0.5 text-sm">
          {(["en", "ru"] as const).map((item) => (
            <Link
              key={item}
              href={`/assessment/language/${item}`}
              hrefLang={item}
              aria-current={language === item ? "page" : undefined}
              className={cn(
                "min-h-9 rounded-md px-3 py-2 uppercase focus-visible:ring-2 focus-visible:ring-ring",
                language === item ? "bg-bg-active text-text" : "text-text-muted hover:text-text",
              )}
            >
              {item}
            </Link>
          ))}
        </nav>
      </div>
      {children}
    </main>
  );
}

function QuestionField({
  question,
  number,
  value,
  error,
  language,
  attemptId,
  onChange,
  onRemove,
}: {
  question: PublicQuestion;
  number: number;
  value: unknown;
  error: string | null;
  language: "en" | "ru";
  attemptId: string;
  onChange: (value: unknown) => void;
  onRemove: () => void;
}) {
  const t = COPY[language];
  const descriptionId = `question-${question.id}-description`;
  const errorId = `question-${question.id}-error`;
  const describedBy = [question.description ? descriptionId : null, error ? errorId : null]
    .filter(Boolean)
    .join(" ") || undefined;
  const options = optionList(question);
  const baseInput =
    "min-h-11 w-full rounded-lg border border-input bg-bg px-3 py-2.5 text-base outline-none transition-colors placeholder:text-text-faint focus:border-ring focus:ring-4 focus:ring-ring/30 disabled:opacity-60";

  return (
    <fieldset
      id={`question-${question.id}`}
      tabIndex={-1}
      aria-describedby={describedBy}
      className={cn(
        "rounded-xl border bg-bg-elevated p-4 outline-none transition-colors sm:p-5",
        error ? "border-destructive/60 focus:ring-4 focus:ring-destructive/20" : "border-border focus:ring-4 focus:ring-ring/20",
      )}
    >
      <legend className="float-left w-full p-0">
        <span className="flex items-start gap-3">
          <span className="mt-0.5 flex h-7 min-w-7 items-center justify-center rounded-full bg-bg-active px-1 font-mono text-xs text-text-muted">
            {number}
          </span>
          <span className="min-w-0 flex-1 text-base font-semibold leading-7">
            {question.prompt}
            <span className={cn("ml-2 text-xs font-medium", question.required ? "text-primary" : "text-text-muted")}>
              {question.required ? t.required : t.optional}
            </span>
          </span>
        </span>
      </legend>
      <div className="clear-both pt-3 pl-10">
        {question.description ? (
          <p id={descriptionId} className="mb-3 text-sm leading-6 text-text-muted">
            {question.description}
          </p>
        ) : null}

        {question.question_type === "boolean" ? (
          <div className="grid gap-2 sm:grid-cols-2">
            {[
              { option: true, label: t.yes },
              { option: false, label: t.no },
            ].map((item) => (
              <label key={String(item.option)} className="flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border border-border px-3 py-2 hover:bg-bg-hover focus-within:ring-4 focus-within:ring-ring/30">
                <input
                  type="radio"
                  name={question.id}
                  checked={value === item.option}
                  onChange={() => onChange(item.option)}
                  aria-invalid={!!error}
                  className="h-5 w-5 accent-primary"
                />
                <span>{item.label}</span>
              </label>
            ))}
          </div>
        ) : question.question_type === "single" ? (
          <div role="radiogroup" aria-label={t.selectOne} className="space-y-2">
            {options.map((option) => (
              <label key={option.value} className="flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border border-border px-3 py-2 hover:bg-bg-hover focus-within:ring-4 focus-within:ring-ring/30">
                <input
                  type="radio"
                  name={question.id}
                  value={option.value}
                  checked={value === option.value}
                  onChange={() => onChange(option.value)}
                  aria-invalid={!!error}
                  className="h-5 w-5 accent-primary"
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
        ) : question.question_type === "multi" ? (
          <div aria-label={t.selectMany} className="space-y-2">
            {options.map((option) => {
              const selected = Array.isArray(value) && value.includes(option.value);
              return (
                <label key={option.value} className="flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border border-border px-3 py-2 hover:bg-bg-hover focus-within:ring-4 focus-within:ring-ring/30">
                  <input
                    type="checkbox"
                    value={option.value}
                    checked={selected}
                    onChange={() => {
                      const current = Array.isArray(value)
                        ? value.filter((item): item is string => typeof item === "string")
                        : [];
                      onChange(
                        selected
                          ? current.filter((item) => item !== option.value)
                          : [...current, option.value],
                      );
                    }}
                    aria-invalid={!!error}
                    className="h-5 w-5 rounded accent-primary"
                  />
                  <span>{option.label}</span>
                </label>
              );
            })}
          </div>
        ) : question.question_type === "number" ? (
          <input
            type="number"
            inputMode="decimal"
            step="any"
            min={validationNumber(question, "min_value")}
            max={validationNumber(question, "max_value")}
            value={typeof value === "number" || typeof value === "string" ? value : ""}
            onChange={(event) => {
              onChange(event.target.value === "" ? null : Number(event.target.value));
            }}
            placeholder={t.number}
            aria-invalid={!!error}
            className={baseInput}
          />
        ) : question.question_type === "long" ? (
          <textarea
            value={typeof value === "string" ? value : ""}
            onChange={(event) => onChange(event.target.value || null)}
            minLength={validationNumber(question, "min_length")}
            maxLength={validationNumber(question, "max_length") ?? 100_000}
            rows={7}
            placeholder={t.long}
            aria-invalid={!!error}
            className={cn(baseInput, "min-h-36 resize-y")}
          />
        ) : question.question_type === "url" ? (
          <input
            type="url"
            inputMode="url"
            autoCapitalize="none"
            autoCorrect="off"
            value={typeof value === "string" ? value : ""}
            onChange={(event) => onChange(event.target.value || null)}
            minLength={validationNumber(question, "min_length")}
            maxLength={validationNumber(question, "max_length") ?? 2_048}
            placeholder={t.url}
            aria-invalid={!!error}
            className={baseInput}
          />
        ) : question.question_type === "file" ? (
          <FileQuestion
            question={question}
            attemptId={attemptId}
            value={value}
            language={language}
            onChange={onChange}
            onRemove={onRemove}
          />
        ) : (
          <input
            type="text"
            value={typeof value === "string" ? value : ""}
            onChange={(event) => onChange(event.target.value || null)}
            minLength={validationNumber(question, "min_length")}
            maxLength={validationNumber(question, "max_length") ?? 10_000}
            placeholder={t.short}
            aria-invalid={!!error}
            className={baseInput}
          />
        )}

        {question.question_type !== "file" && answerIsPresent(value) ? (
          <button
            type="button"
            onClick={() => onChange(null)}
            className="mt-3 min-h-10 rounded-md px-2 text-sm font-medium text-text-muted underline-offset-4 hover:text-text hover:underline focus-visible:ring-2 focus-visible:ring-ring"
          >
            {t.clearAnswer}
          </button>
        ) : null}

        {error ? (
          <p id={errorId} role="alert" className="mt-2 text-sm font-medium text-destructive">
            {error}
          </p>
        ) : null}
      </div>
    </fieldset>
  );
}

function FileQuestion({
  question,
  attemptId,
  value,
  language,
  onChange,
  onRemove,
}: {
  question: PublicQuestion;
  attemptId: string;
  value: unknown;
  language: "en" | "ru";
  onChange: (value: unknown) => void;
  onRemove: () => void;
}) {
  const t = COPY[language];
  const [uploading, setUploading] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileValue =
    value && typeof value === "object" && "filename" in value
      ? (value as { filename?: unknown; size_bytes?: unknown })
      : null;
  const extensions = Array.isArray(question.validation.allowed_file_extensions)
    ? question.validation.allowed_file_extensions.filter(
        (item): item is string => typeof item === "string",
      )
    : [];
  const maximum = validationNumber(question, "max_file_size_bytes");

  function fileLimit(bytes: number): string {
    const formatter = new Intl.NumberFormat(language === "ru" ? "ru-RU" : "en-US", {
      maximumFractionDigits: 1,
    });
    if (bytes < 1024) return `${formatter.format(bytes)} B`;
    if (bytes < 1_048_576) return `${formatter.format(bytes / 1024)} KiB`;
    return `${formatter.format(bytes / 1_048_576)} MiB`;
  }

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (maximum && file.size > maximum) {
      const limit = fileLimit(maximum);
      setError(
        language === "ru"
          ? `Файл превышает лимит ${limit}.`
          : `File exceeds the ${limit} limit.`,
      );
      return;
    }
    const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
    if (extensions.length > 0 && !extensions.includes(extension)) {
      setError(`Allowed: ${extensions.join(", ")}`);
      return;
    }
    setUploading(true);
    setError(null);
    try {
      const body = new FormData();
      body.append("question_id", question.id);
      body.append("file", file);
      const uploaded = await responseData<AssessmentFileUpload>(
        await fetch(
          `/api/assessment/attempt/${encodeURIComponent(attemptId)}/files`,
          { method: "POST", body },
        ),
        t.serviceError,
      );
      onChange(uploaded);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : t.serviceError);
    } finally {
      setUploading(false);
    }
  }

  async function remove() {
    if (!fileValue || typeof (value as { file_id?: unknown }).file_id !== "string") return;
    setRemoving(true);
    setError(null);
    try {
      const response = await fetch(
        `/api/assessment/attempt/${encodeURIComponent(attemptId)}/files/${encodeURIComponent((value as { file_id: string }).file_id)}`,
        { method: "DELETE" },
      );
      if (!response.ok) {
        throw new Error(t.serviceError);
      }
      onRemove();
    } catch (removeError) {
      setError(removeError instanceof Error ? removeError.message : t.serviceError);
    } finally {
      setRemoving(false);
    }
  }

  if (fileValue && typeof fileValue.filename === "string") {
    return (
      <div className="flex flex-col gap-3 rounded-lg border border-success/30 bg-success/10 p-3 sm:flex-row sm:items-center">
        <CheckCircle2 className="h-5 w-5 shrink-0 text-success" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{fileValue.filename}</p>
          <p className="text-xs text-text-muted">
            {typeof fileValue.size_bytes === "number"
              ? `${Math.ceil(fileValue.size_bytes / 1024)} KB · ${t.fileUploaded}`
              : t.fileUploaded}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void remove()}
          disabled={removing}
          className="min-h-9 rounded-lg border border-border bg-bg px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50"
        >
          {removing ? t.fileUploading : t.removeFile}
        </button>
      </div>
    );
  }

  return (
    <div>
      <label className="flex min-h-12 cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-border px-4 py-3 font-medium hover:bg-bg-hover focus-within:ring-4 focus-within:ring-ring/30">
        {uploading ? (
          <Loader2 className="h-5 w-5 animate-spin motion-reduce:animate-none" />
        ) : (
          <FileUp className="h-5 w-5" />
        )}
        <span>{uploading ? t.fileUploading : t.file}</span>
        <input
          type="file"
          disabled={uploading}
          accept={extensions.length ? extensions.map((item) => `.${item}`).join(",") : undefined}
          onChange={(event) => void upload(event)}
          className="sr-only"
        />
      </label>
      {error ? <p role="alert" className="mt-2 text-sm text-destructive">{error}</p> : null}
    </div>
  );
}

function ResultSummary({
  submission,
  language,
}: {
  submission: PublicAssessmentSubmission;
  language: "en" | "ru";
}) {
  const t = COPY[language];
  const result = submission.result;
  const pendingReview = result.needs_manual_review;
  return (
    <div className="mt-7 rounded-xl border border-border bg-bg p-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        {pendingReview ? (
          <p className="text-sm font-medium text-text">{t.manualReview}</p>
        ) : (
          <div>
            <p className="text-sm text-text-muted">{t.score}</p>
            <p className="mt-1 font-mono text-3xl font-semibold tabular-nums">
              {Number(result.percentage).toFixed(1)}%
            </p>
          </div>
        )}
        <span
          className={cn(
            "rounded-full px-3 py-1.5 text-sm font-semibold",
            !pendingReview && result.passed
              ? "bg-success/15 text-success"
              : "bg-warning/15 text-warning",
          )}
        >
          {pendingReview ? t.manualReview : result.passed ? t.passed : t.notPassed}
        </span>
      </div>
    </div>
  );
}

function PublicResultSummary({
  result,
  language,
}: {
  result: PublicAssessmentResult;
  language: "en" | "ru";
}) {
  const t = COPY[language];
  return (
    <div className="mt-7 rounded-xl border border-border bg-bg p-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-sm text-text-muted">{t.score}</p>
          <p className="mt-1 font-mono text-3xl font-semibold tabular-nums">
            {Number(result.percentage).toFixed(1)}%
          </p>
        </div>
        <span
          className={cn(
            "rounded-full px-3 py-1.5 text-sm font-semibold",
            result.passed ? "bg-success/15 text-success" : "bg-warning/15 text-warning",
          )}
        >
          {result.passed ? t.passed : t.notPassed}
        </span>
      </div>
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-text-muted">{label}</dt>
      <dd className="mt-1 text-sm font-medium">{value}</dd>
    </div>
  );
}

function formatDate(value: string, language: "en" | "ru") {
  return new Intl.DateTimeFormat(language === "ru" ? "ru-RU" : "en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatTime(value: string, language: "en" | "ru") {
  return new Intl.DateTimeFormat(language === "ru" ? "ru-RU" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
