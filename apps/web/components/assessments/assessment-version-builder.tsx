"use client";

import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Braces,
  ChevronDown,
  ChevronUp,
  FilePlus2,
  Loader2,
  Plus,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState, useTransition } from "react";
import { toast } from "sonner";

import { publishAssessmentVersionAction } from "@/app/(app)/assessments/actions";
import {
  OPERATORS_BY_TYPE,
  QUESTION_TYPES,
  conditionRules,
  defaultRuleValue,
  draftFingerprint,
  initialQuestion,
  initialSection,
  isConditionGroup,
  operatorNeedsValue,
  parseDraft,
  serializeDraft,
  stateFromPublishedVersion,
  stateFromTemplate,
  type AssessmentTemplate,
  type AssessmentVersion,
  type BuilderState,
  type ConditionOperator,
  type ConditionRule,
  type DraftQuestion,
  type DraftSection,
  type Knockout,
  type PreviousQuestion,
  type QuestionInput,
  type QuestionType,
  type Scoring,
  type SectionInput,
  type VersionInput,
} from "@/components/assessments/assessment-builder-model";
import { cn } from "@/lib/utils";

const COPY = {
  en: {
    title: "Publish a new version",
    immutable:
      "Published versions are immutable. Build and review the complete question graph before publishing.",
    basedOn: "Based on published v{version}. Publishing creates v{next}.",
    blankBase: "Starting from a blank draft for v1.",
    recovered: "Recovered a browser draft saved {date}.",
    draftSaved: "Draft saved in this browser.",
    discardDraft: "Discard draft",
    discardConfirm: "Discard all unpublished changes and return to the latest published version?",
    versionTitle: "Version title",
    instructions: "Candidate instructions",
    passing: "Passing percentage",
    timeLimit: "Time limit (minutes)",
    noLimit: "Leave blank for no limit",
    section: "Section",
    sectionTitle: "Section title",
    sectionDescription: "Section description",
    addSection: "Add section",
    deleteSection: "Delete section",
    moveSectionUp: "Move section up",
    moveSectionDown: "Move section down",
    question: "Question",
    addQuestion: "Add question",
    deleteQuestion: "Delete question",
    moveQuestionUp: "Move question up",
    moveQuestionDown: "Move question down",
    key: "Stable key",
    type: "Answer type",
    prompt: "Question prompt",
    description: "Help text",
    required: "Required when visible",
    options: "Options (one value | label per line)",
    validation: "Validation",
    minLength: "Minimum length",
    maxLength: "Maximum length",
    minValue: "Minimum value",
    maxValue: "Maximum value",
    fileExtensions: "Allowed extensions",
    fileSize: "Maximum file size (MB)",
    branching: "Conditional visibility",
    showWhen: "Show when",
    matchRules: "Rule matching",
    allRules: "All rules match",
    anyRule: "Any rule matches",
    addRule: "Add rule",
    removeRule: "Remove rule",
    sourceQuestion: "Earlier question",
    operator: "Operator",
    expected: "Expected value",
    valueHint: "Use true, false, a number, or JSON such as [\"a\",\"b\"]",
    scoring: "Automatic scoring",
    scored: "Score this question",
    maxScore: "Maximum score",
    correct: "Correct value",
    accepted: "Accepted answers (comma separated)",
    tolerance: "Numeric tolerance",
    exactMode: "Exact value",
    rangeMode: "Numeric range",
    partial: "Partial credit for multiple choice",
    caseSensitive: "Case-sensitive text matching",
    knockout: "Knockout rule",
    knockoutEnabled: "Flag for human review when this rule matches",
    reason: "Internal knockout reason",
    publish: "Review publication",
    publishing: "Publishing…",
    confirmTitle: "Publish this immutable version?",
    confirmBody: "After publication, create a new version to make any changes.",
    confirm: "Publish version",
    cancel: "Keep editing",
    errors: "Fix the following before publishing:",
    automaticScoreRequired: "Add at least one question with a maximum score.",
    invalidOperator: "Choose an operator compatible with the answer type.",
    invalidRange: "The numeric scoring minimum cannot exceed the maximum.",
    published: "Assessment version published",
    collapse: "Collapse question",
    expand: "Expand question",
  },
  ru: {
    title: "Опубликовать новую версию",
    immutable:
      "Опубликованные версии неизменяемы. Перед публикацией проверьте все вопросы и условные переходы.",
    basedOn: "Основано на опубликованной версии {version}. Публикация создаст версию {next}.",
    blankBase: "Начат пустой черновик для версии 1.",
    recovered: "Восстановлен черновик из браузера, сохранённый {date}.",
    draftSaved: "Черновик сохранён в этом браузере.",
    discardDraft: "Удалить черновик",
    discardConfirm: "Удалить все неопубликованные изменения и вернуться к последней опубликованной версии?",
    versionTitle: "Название версии",
    instructions: "Инструкция кандидату",
    passing: "Проходной процент",
    timeLimit: "Лимит времени (минуты)",
    noLimit: "Оставьте пустым, если лимита нет",
    section: "Раздел",
    sectionTitle: "Название раздела",
    sectionDescription: "Описание раздела",
    addSection: "Добавить раздел",
    deleteSection: "Удалить раздел",
    moveSectionUp: "Поднять раздел",
    moveSectionDown: "Опустить раздел",
    question: "Вопрос",
    addQuestion: "Добавить вопрос",
    deleteQuestion: "Удалить вопрос",
    moveQuestionUp: "Поднять вопрос",
    moveQuestionDown: "Опустить вопрос",
    key: "Стабильный ключ",
    type: "Тип ответа",
    prompt: "Текст вопроса",
    description: "Подсказка",
    required: "Обязателен, когда отображается",
    options: "Варианты (значение | подпись, по одному в строке)",
    validation: "Проверка ответа",
    minLength: "Минимальная длина",
    maxLength: "Максимальная длина",
    minValue: "Минимальное значение",
    maxValue: "Максимальное значение",
    fileExtensions: "Разрешённые расширения",
    fileSize: "Максимальный размер файла (МБ)",
    branching: "Условное отображение",
    showWhen: "Показывать при условии",
    matchRules: "Совпадение условий",
    allRules: "Выполнены все условия",
    anyRule: "Выполнено любое условие",
    addRule: "Добавить условие",
    removeRule: "Удалить условие",
    sourceQuestion: "Предыдущий вопрос",
    operator: "Оператор",
    expected: "Ожидаемое значение",
    valueHint: "Используйте true, false, число или JSON, например [\"a\",\"b\"]",
    scoring: "Автоматическая оценка",
    scored: "Начислять баллы за вопрос",
    maxScore: "Максимальный балл",
    correct: "Правильный ответ",
    accepted: "Допустимые ответы (через запятую)",
    tolerance: "Допуск для числа",
    exactMode: "Точное значение",
    rangeMode: "Числовой диапазон",
    partial: "Частичный балл за множественный выбор",
    caseSensitive: "Учитывать регистр текста",
    knockout: "Отсеивающее условие",
    knockoutEnabled: "Отправить на ручную проверку при совпадении условия",
    reason: "Внутренняя причина отсева",
    publish: "Проверить публикацию",
    publishing: "Публикация…",
    confirmTitle: "Опубликовать неизменяемую версию?",
    confirmBody: "После публикации для любых изменений потребуется новая версия.",
    confirm: "Опубликовать версию",
    cancel: "Продолжить редактирование",
    errors: "Исправьте ошибки перед публикацией:",
    automaticScoreRequired: "Добавьте хотя бы один вопрос с максимальным баллом.",
    invalidOperator: "Выберите оператор, совместимый с типом ответа.",
    invalidRange: "Минимум числового диапазона не может превышать максимум.",
    published: "Версия оценивания опубликована",
    collapse: "Свернуть вопрос",
    expand: "Развернуть вопрос",
  },
} as const;

function newId(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`;
}

function parseOptions(raw: string) {
  return raw.split("\n").flatMap((line) => {
    const [rawValue, ...labelParts] = line.split("|");
    const value = rawValue?.trim();
    const label = labelParts.join("|").trim() || value;
    return value && label ? [{ value, label }] : [];
  });
}

function optionsText(question: QuestionInput): string {
  return (question.options ?? []).map((option) => `${option.value} | ${option.label}`).join("\n");
}

function finiteNumber(value: string): number | undefined {
  if (!value.trim()) return undefined;
  const number = Number(value);
  return Number.isFinite(number) ? number : undefined;
}

function hasFiniteNumericValue(value: unknown): boolean {
  if (typeof value === "boolean" || value == null || value === "") return false;
  return Number.isFinite(Number(value));
}

function ruleValueIsCompatible(
  question: PreviousQuestion,
  operator: ConditionOperator,
  value: unknown,
): boolean {
  if (!OPERATORS_BY_TYPE[question.question_type].includes(operator)) return false;
  if (!operatorNeedsValue(operator)) return value == null;
  const options = new Set((question.options ?? []).map((option) => option.value));
  if (question.question_type === "boolean") return typeof value === "boolean";
  if (question.question_type === "single") {
    return operator === "in"
      ? Array.isArray(value) && value.length > 0 && value.every((item) => typeof item === "string" && options.has(item))
      : typeof value === "string" && options.has(value);
  }
  if (question.question_type === "multi") {
    return typeof value === "string" && options.has(value);
  }
  if (question.question_type === "number") return hasFiniteNumericValue(value);
  if (operator === "in") {
    return Array.isArray(value) && value.length > 0 && value.every((item) => typeof item === "string");
  }
  return typeof value === "string";
}

export function AssessmentVersionBuilder({
  template,
  language,
  draftScope,
  onPublished,
}: {
  template: AssessmentTemplate;
  language: "en" | "ru";
  draftScope: string;
  onPublished: (version: AssessmentVersion) => void;
}) {
  const t = COPY[language];
  const [seed] = useState(() => stateFromTemplate(template));
  const [baseline, setBaseline] = useState(seed);
  const [title, setTitle] = useState(seed.title);
  const [instructions, setInstructions] = useState(seed.instructions);
  const [passing, setPassing] = useState(seed.passing);
  const [timeLimit, setTimeLimit] = useState(seed.timeLimit);
  const [sections, setSections] = useState<DraftSection[]>(seed.sections);
  const [baseVersion, setBaseVersion] = useState(seed.baseVersion);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<string[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [draftReady, setDraftReady] = useState(false);
  const [recoveredAt, setRecoveredAt] = useState<string | null>(null);
  const [draftSavedAt, setDraftSavedAt] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const draftKey = `recruiter:assessment-draft:v1:${draftScope}:${template.id}`;
  const builderState = useMemo<BuilderState>(
    () => ({
      title,
      instructions,
      passing,
      timeLimit,
      sections,
      baseVersion,
    }),
    [baseVersion, instructions, passing, sections, timeLimit, title],
  );
  const fingerprint = useMemo(() => draftFingerprint(builderState), [builderState]);
  const baselineFingerprint = useMemo(() => draftFingerprint(baseline), [baseline]);
  const dirty = fingerprint !== baselineFingerprint;
  const flattened = sections.flatMap((section) => section.questions);

  function applyBuilderState(next: BuilderState) {
    setTitle(next.title);
    setInstructions(next.instructions);
    setPassing(next.passing);
    setTimeLimit(next.timeLimit);
    setSections(next.sections);
    setBaseVersion(next.baseVersion);
    setCollapsed(new Set());
    setErrors([]);
    setConfirming(false);
  }

  useEffect(() => {
    let raw: string | null = null;
    try {
      raw = window.localStorage.getItem(draftKey);
    } catch {
      // Storage can be unavailable in hardened/private browser contexts.
    }
    if (raw) {
      const recovered = parseDraft(raw, template.id);
      if (recovered) {
        applyBuilderState(recovered.state);
        setRecoveredAt(recovered.savedAt);
      } else {
        try {
          window.localStorage.removeItem(draftKey);
        } catch {
          // The builder remains usable without browser persistence.
        }
      }
    }
    setDraftReady(true);
    // This component is keyed by template id, so recovery runs once per draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey, template.id]);

  useEffect(() => {
    if (!draftReady) return;
    if (!dirty) {
      try {
        window.localStorage.removeItem(draftKey);
      } catch {
        // The builder remains usable without browser persistence.
      }
      setDraftSavedAt(null);
      return;
    }
    const timeout = window.setTimeout(() => {
      const savedAt = new Date().toISOString();
      try {
        window.localStorage.setItem(
          draftKey,
          serializeDraft(template.id, builderState, savedAt),
        );
        setDraftSavedAt(savedAt);
      } catch {
        // Publishing is still available if quota or policy blocks storage.
      }
    }, 500);
    return () => window.clearTimeout(timeout);
  }, [builderState, dirty, draftKey, draftReady, template.id]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  function discardDraft() {
    if (!window.confirm(t.discardConfirm)) return;
    try {
      window.localStorage.removeItem(draftKey);
    } catch {
      // Resetting the in-memory draft still succeeds.
    }
    applyBuilderState(baseline);
    setRecoveredAt(null);
    setDraftSavedAt(null);
  }

  function patchQuestion(clientId: string, patch: Partial<QuestionInput>) {
    setSections((current) =>
      current.map((section) => ({
        ...section,
        questions: section.questions.map((question) =>
          question.clientId === clientId
            ? { ...question, value: { ...question.value, ...patch } }
            : question,
        ),
      })),
    );
  }

  function addSection() {
    setSections((current) => [...current, initialSection(newId("section"))]);
  }

  function removeSection(clientId: string) {
    setSections((current) => current.filter((section) => section.clientId !== clientId));
  }

  function moveSection(index: number, direction: -1 | 1) {
    setSections((current) => move(current, index, direction));
  }

  function patchSection(clientId: string, patch: Partial<Pick<DraftSection, "title" | "description">>) {
    setSections((current) =>
      current.map((section) =>
        section.clientId === clientId ? { ...section, ...patch } : section,
      ),
    );
  }

  function addQuestion(sectionId: string) {
    setSections((current) =>
      current.map((section) =>
        section.clientId === sectionId
          ? {
              ...section,
              questions: [
                ...section.questions,
                initialQuestion(newId("question"), flattened.length),
              ],
            }
          : section,
      ),
    );
  }

  function removeQuestion(sectionId: string, questionId: string) {
    setSections((current) =>
      current.map((section) =>
        section.clientId === sectionId
          ? {
              ...section,
              questions: section.questions.filter(
                (question) => question.clientId !== questionId,
              ),
            }
          : section,
      ),
    );
  }

  function moveQuestion(sectionId: string, index: number, direction: -1 | 1) {
    setSections((current) =>
      current.map((section) =>
        section.clientId === sectionId
          ? { ...section, questions: move(section.questions, index, direction) }
          : section,
      ),
    );
  }

  function validate(): string[] {
    const problems: string[] = [];
    if (!title.trim()) problems.push(t.versionTitle);
    if (sections.length === 0) problems.push(t.addSection);
    const keys = new Set<string>();
    const known = new Map<string, PreviousQuestion>();
    let totalMaximum = 0;
    sections.forEach((section, sectionIndex) => {
      if (!section.title.trim()) problems.push(`${t.section} ${sectionIndex + 1}: ${t.sectionTitle}`);
      if (section.questions.length === 0) problems.push(`${t.section} ${sectionIndex + 1}: ${t.addQuestion}`);
      section.questions.forEach((question, questionIndex) => {
        const prefix = `${t.section} ${sectionIndex + 1}, ${t.question} ${questionIndex + 1}`;
        if (!question.value.key.trim()) problems.push(`${prefix}: ${t.key}`);
        if (!question.value.prompt.trim()) problems.push(`${prefix}: ${t.prompt}`);
        if (keys.has(question.value.key)) problems.push(`${prefix}: duplicate key ${question.value.key}`);
        if (
          (question.value.question_type === "single" || question.value.question_type === "multi") &&
          (question.value.options ?? []).length < 2
        ) {
          problems.push(`${prefix}: ${t.options}`);
        }
        if (Number(question.value.max_score) > 0) {
          totalMaximum += Number(question.value.max_score);
        }
        if (question.value.scoring) {
          const optionValues = new Set(
            (question.value.options ?? []).map((option) => option.value),
          );
          const correct = question.value.scoring.correct;
          if (Number(question.value.max_score) <= 0) {
            problems.push(`${prefix}: ${t.maxScore}`);
          }
          if (
            question.value.question_type === "single" &&
            (typeof correct !== "string" || !optionValues.has(correct))
          ) {
            problems.push(`${prefix}: ${t.correct}`);
          }
          if (
            question.value.question_type === "multi" &&
            (!Array.isArray(correct) ||
              correct.length === 0 ||
              correct.some((item) => typeof item !== "string" || !optionValues.has(item)))
          ) {
            problems.push(`${prefix}: ${t.correct}`);
          }
          if (
            (question.value.question_type === "short" ||
              question.value.question_type === "long" ||
              question.value.question_type === "url") &&
            (question.value.scoring.accepted_values ?? []).length === 0
          ) {
            problems.push(`${prefix}: ${t.accepted}`);
          }
          if (question.value.question_type === "number") {
            const scoring = question.value.scoring;
            const hasExact = scoring.correct !== null && scoring.correct !== undefined;
            const hasMinimum = scoring.min_value !== null && scoring.min_value !== undefined;
            const hasMaximum = scoring.max_value !== null && scoring.max_value !== undefined;
            const hasRange = hasMinimum || hasMaximum;
            if (hasExact === hasRange) {
              problems.push(`${prefix}: ${t.exactMode} / ${t.rangeMode}`);
            } else if (hasExact) {
              if (!hasFiniteNumericValue(scoring.correct)) {
                problems.push(`${prefix}: ${t.correct}`);
              }
              if (
                scoring.tolerance != null &&
                (!hasFiniteNumericValue(scoring.tolerance) || Number(scoring.tolerance) < 0)
              ) {
                problems.push(`${prefix}: ${t.tolerance}`);
              }
            } else if (
              !hasMinimum ||
              !hasMaximum ||
              !hasFiniteNumericValue(scoring.min_value) ||
              !hasFiniteNumericValue(scoring.max_value)
            ) {
              problems.push(`${prefix}: ${t.rangeMode}`);
            } else if (Number(scoring.min_value) > Number(scoring.max_value)) {
              problems.push(`${prefix}: ${t.invalidRange}`);
            } else if (scoring.tolerance != null) {
              problems.push(`${prefix}: ${t.tolerance}`);
            }
          }
        }
        if (question.value.condition) {
          for (const rule of conditionRules(question.value.condition)) {
            const source = known.get(rule.question_key);
            if (!source) {
              problems.push(`${prefix}: ${t.sourceQuestion}`);
            } else if (
              !OPERATORS_BY_TYPE[source.question_type].includes(rule.operator)
            ) {
              problems.push(`${prefix}: ${t.invalidOperator}`);
            } else if (
              !ruleValueIsCompatible(source, rule.operator, rule.value)
            ) {
              problems.push(`${prefix}: ${t.expected}`);
            }
          }
        }
        if (question.value.knockout) {
          if (
            !OPERATORS_BY_TYPE[question.value.question_type].includes(
              question.value.knockout.operator,
            )
          ) {
            problems.push(`${prefix}: ${t.invalidOperator}`);
          } else if (
            !ruleValueIsCompatible(
              question.value,
              question.value.knockout.operator,
              question.value.knockout.value,
            )
          ) {
            problems.push(`${prefix}: ${t.expected}`);
          }
        }
        keys.add(question.value.key);
        known.set(question.value.key, question.value);
      });
    });
    if (totalMaximum <= 0) problems.push(t.automaticScoreRequired);
    return problems;
  }

  function review() {
    const problems = validate();
    setErrors(problems);
    if (problems.length) {
      document.getElementById("assessment-builder-errors")?.focus();
      return;
    }
    setConfirming(true);
  }

  function publish() {
    const body: VersionInput = {
      title: title.trim(),
      instructions: instructions.trim() || null,
      passing_percentage: passing,
      time_limit_minutes: timeLimit.trim() ? Number(timeLimit) : null,
      sections: sections.map<SectionInput>((section) => ({
        title: section.title.trim(),
        description: section.description.trim() || null,
        questions: section.questions.map((question) => normalizeQuestion(question.value)),
      })),
    };
    startTransition(async () => {
      const result = await publishAssessmentVersionAction(template.id, body);
      if (!result.ok) {
        setErrors([result.error]);
        setConfirming(false);
        return;
      }
      const next = stateFromPublishedVersion(result.data);
      try {
        window.localStorage.removeItem(draftKey);
      } catch {
        // The published version is authoritative even if storage is unavailable.
      }
      onPublished(result.data);
      setBaseline(next);
      applyBuilderState(next);
      setRecoveredAt(null);
      setDraftSavedAt(null);
      toast.success(t.published);
    });
  }

  return (
    <section className="rounded-xl border border-border bg-bg-elevated p-4 sm:p-5">
      <div className="flex items-start gap-3">
        <Braces className="mt-0.5 h-5 w-5 text-primary" aria-hidden />
        <div>
          <h2 className="text-lg font-semibold">{t.title}</h2>
          <p className="mt-1 text-sm leading-6 text-text-muted">{t.immutable}</p>
          <p
            data-testid="assessment-builder-base-version"
            className="mt-1 text-sm font-medium text-text"
          >
            {baseVersion
              ? t.basedOn
                  .replace("{version}", String(baseVersion.number))
                  .replace("{next}", String(baseVersion.number + 1))
              : t.blankBase}
          </p>
        </div>
      </div>

      {recoveredAt ? (
        <div
          role="status"
          className="mt-4 flex flex-col gap-3 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm sm:flex-row sm:items-center sm:justify-between"
        >
          <span>
            {t.recovered.replace(
              "{date}",
              new Intl.DateTimeFormat(language === "ru" ? "ru-RU" : "en-US", {
                dateStyle: "medium",
                timeStyle: "short",
              }).format(new Date(recoveredAt)),
            )}
          </span>
          <button
            type="button"
            onClick={discardDraft}
            className="min-h-9 shrink-0 rounded-lg border border-border bg-bg px-3 py-2 font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring"
          >
            {t.discardDraft}
          </button>
        </div>
      ) : dirty ? (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm">
          <span aria-live="polite" className="text-text-muted">
            {draftSavedAt ? t.draftSaved : ""}
          </span>
          <button
            type="button"
            onClick={discardDraft}
            className="min-h-9 rounded-lg border border-border px-3 py-2 font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring"
          >
            {t.discardDraft}
          </button>
        </div>
      ) : null}

      {errors.length ? (
        <div
          id="assessment-builder-errors"
          role="alert"
          tabIndex={-1}
          className="mt-5 rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive outline-none focus:ring-3 focus:ring-destructive/20"
        >
          <p className="font-semibold">{t.errors}</p>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            {errors.map((error, index) => <li key={`${error}-${index}`}>{error}</li>)}
          </ul>
        </div>
      ) : null}

      <div className="mt-5 grid gap-4 sm:grid-cols-2">
        <BuilderField label={t.versionTitle} htmlFor="assessment-version-title" wide>
          <input id="assessment-version-title" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={200} required className={INPUT} />
        </BuilderField>
        <BuilderField label={t.instructions} htmlFor="assessment-version-instructions" wide>
          <textarea id="assessment-version-instructions" value={instructions} onChange={(event) => setInstructions(event.target.value)} maxLength={20_000} rows={4} className={TEXTAREA} />
        </BuilderField>
        <BuilderField label={t.passing} htmlFor="assessment-version-passing">
          <input id="assessment-version-passing" type="number" min={0} max={100} step="0.1" value={passing} onChange={(event) => setPassing(Number(event.target.value))} className={INPUT} />
        </BuilderField>
        <BuilderField label={t.timeLimit} hint={t.noLimit} htmlFor="assessment-version-time-limit">
          <input id="assessment-version-time-limit" type="number" min={1} max={10_080} value={timeLimit} onChange={(event) => setTimeLimit(event.target.value)} className={INPUT} />
        </BuilderField>
      </div>

      <div className="mt-6 space-y-5">
        {sections.map((section, sectionIndex) => (
          <section key={section.clientId} className="rounded-xl border border-border bg-bg p-4">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="mr-auto font-semibold">{t.section} {sectionIndex + 1}</h3>
              <IconButton label={t.moveSectionUp} disabled={sectionIndex === 0} onClick={() => moveSection(sectionIndex, -1)}><ArrowUp /></IconButton>
              <IconButton label={t.moveSectionDown} disabled={sectionIndex === sections.length - 1} onClick={() => moveSection(sectionIndex, 1)}><ArrowDown /></IconButton>
              <IconButton label={t.deleteSection} disabled={sections.length === 1} destructive onClick={() => removeSection(section.clientId)}><Trash2 /></IconButton>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <BuilderField label={t.sectionTitle} htmlFor={`${section.clientId}-title`}>
                <input id={`${section.clientId}-title`} value={section.title} onChange={(event) => patchSection(section.clientId, { title: event.target.value })} maxLength={200} className={INPUT} />
              </BuilderField>
              <BuilderField label={t.sectionDescription} htmlFor={`${section.clientId}-description`}>
                <input id={`${section.clientId}-description`} value={section.description} onChange={(event) => patchSection(section.clientId, { description: event.target.value })} maxLength={10_000} className={INPUT} />
              </BuilderField>
            </div>

            <div className="mt-4 space-y-3">
              {section.questions.map((question, questionIndex) => {
                const flatIndex = flattened.findIndex((item) => item.clientId === question.clientId);
                const previousQuestions = flattened
                  .slice(0, flatIndex)
                  .map((item) => item.value)
                  .filter((item) => item.key);
                const isCollapsed = collapsed.has(question.clientId);
                return (
                  <QuestionEditor
                    key={question.clientId}
                    question={question}
                    number={questionIndex + 1}
                    language={language}
                    previousQuestions={previousQuestions}
                    collapsed={isCollapsed}
                    onToggle={() => setCollapsed((current) => toggleSet(current, question.clientId))}
                    onPatch={(patch) => patchQuestion(question.clientId, patch)}
                    onMoveUp={() => moveQuestion(section.clientId, questionIndex, -1)}
                    onMoveDown={() => moveQuestion(section.clientId, questionIndex, 1)}
                    onDelete={() => removeQuestion(section.clientId, question.clientId)}
                    canMoveUp={questionIndex > 0}
                    canMoveDown={questionIndex < section.questions.length - 1}
                    canDelete={section.questions.length > 1}
                  />
                );
              })}
            </div>
            <button type="button" onClick={() => addQuestion(section.clientId)} className="mt-3 flex min-h-9 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring">
              <Plus className="h-4 w-4" /> {t.addQuestion}
            </button>
          </section>
        ))}
      </div>

      <button type="button" onClick={addSection} className="mt-4 flex min-h-10 items-center gap-2 rounded-lg border border-dashed border-border px-4 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-3 focus-visible:ring-ring">
        <FilePlus2 className="h-4 w-4" /> {t.addSection}
      </button>

      {confirming ? (
        <div className="mt-6 rounded-xl border border-warning/30 bg-warning/10 p-4">
          <div className="flex gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-warning" />
            <div>
              <h3 className="font-semibold">{t.confirmTitle}</h3>
              <p className="mt-1 text-sm text-text-muted">{t.confirmBody}</p>
            </div>
          </div>
          <div className="mt-4 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button type="button" onClick={() => setConfirming(false)} disabled={pending} className="min-h-10 rounded-lg border border-border px-4 py-2 text-sm font-medium focus-visible:ring-3 focus-visible:ring-ring">{t.cancel}</button>
            <button type="button" onClick={publish} disabled={pending} className="flex min-h-10 items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-50">
              {pending ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : null}
              {pending ? t.publishing : t.confirm}
            </button>
          </div>
        </div>
      ) : (
        <button type="button" onClick={review} className="mt-6 min-h-11 rounded-lg bg-primary px-5 py-2 text-sm font-semibold text-primary-foreground focus-visible:ring-3 focus-visible:ring-ring">
          {t.publish}
        </button>
      )}
    </section>
  );
}

function QuestionEditor({
  question,
  number,
  language,
  previousQuestions,
  collapsed,
  onToggle,
  onPatch,
  onMoveUp,
  onMoveDown,
  onDelete,
  canMoveUp,
  canMoveDown,
  canDelete,
}: {
  question: DraftQuestion;
  number: number;
  language: "en" | "ru";
  previousQuestions: PreviousQuestion[];
  collapsed: boolean;
  onToggle: () => void;
  onPatch: (patch: Partial<QuestionInput>) => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onDelete: () => void;
  canMoveUp: boolean;
  canMoveDown: boolean;
  canDelete: boolean;
}) {
  const t = COPY[language];
  const value = question.value;
  const condition = value.condition;
  const visibilityRules = condition ? conditionRules(condition) : [];
  const conditionMode = condition && isConditionGroup(condition) ? condition.mode : "all";
  const scoring = value.scoring;
  const knockout = value.knockout;
  const supportsOptions = value.question_type === "single" || value.question_type === "multi";
  const textType = value.question_type === "short" || value.question_type === "long" || value.question_type === "url";
  const knockoutOperators = OPERATORS_BY_TYPE[value.question_type];

  function setValidation(key: string, raw: string, transform: (value: string) => unknown = finiteNumber) {
    const validation = { ...(value.validation ?? {}) } as Record<string, unknown>;
    const parsed = transform(raw);
    if (parsed === undefined || parsed === "") delete validation[key];
    else validation[key] = parsed;
    onPatch({ validation });
  }

  function patchScoring(patch: Partial<Scoring>) {
    onPatch({
      scoring: {
        ...defaultScoring(value.question_type, value.options),
        ...(scoring ?? {}),
        ...patch,
      },
    });
  }

  function patchKnockout(patch: Partial<Knockout>) {
    onPatch({
      knockout: {
        operator: knockoutOperators[0],
        ...(knockout ?? {}),
        ...patch,
      },
    });
  }

  function patchVisibilityRule(index: number, nextRule: ConditionRule) {
    onPatch({
      condition: {
        mode: conditionMode,
        rules: visibilityRules.map((rule, ruleIndex) =>
          ruleIndex === index ? nextRule : rule,
        ),
      },
    });
  }

  function addVisibilityRule() {
    const source = previousQuestions.at(-1);
    if (!source || visibilityRules.length >= 20) return;
    const operator = OPERATORS_BY_TYPE[source.question_type][0];
    onPatch({
      condition: {
        mode: conditionMode,
        rules: [
          ...visibilityRules,
          {
            question_key: source.key,
            operator,
            value: defaultRuleValue(source, operator),
          },
        ],
      },
    });
  }

  function removeVisibilityRule(index: number) {
    if (visibilityRules.length <= 1) return;
    onPatch({
      condition: {
        mode: conditionMode,
        rules: visibilityRules.filter((_, ruleIndex) => ruleIndex !== index),
      },
    });
  }

  return (
    <article className="rounded-lg border border-border bg-bg-elevated p-3">
      <div className="flex items-center gap-2">
        <button type="button" onClick={onToggle} aria-expanded={!collapsed} aria-controls={`${question.clientId}-editor`} aria-label={collapsed ? t.expand : t.collapse} className="flex h-8 w-8 items-center justify-center rounded-md hover:bg-bg-hover focus-visible:ring-2 focus-visible:ring-ring">
          {collapsed ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
        </button>
        <span className="font-mono text-xs text-text-muted">{t.question} {number}</span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{value.prompt || value.key}</span>
        <IconButton label={t.moveQuestionUp} disabled={!canMoveUp} onClick={onMoveUp}><ArrowUp /></IconButton>
        <IconButton label={t.moveQuestionDown} disabled={!canMoveDown} onClick={onMoveDown}><ArrowDown /></IconButton>
        <IconButton label={t.deleteQuestion} disabled={!canDelete} destructive onClick={onDelete}><Trash2 /></IconButton>
      </div>
      {!collapsed ? (
        <div id={`${question.clientId}-editor`} className="mt-4 border-t border-border pt-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <BuilderField label={t.key} htmlFor={`${question.clientId}-key`}>
              <input id={`${question.clientId}-key`} value={value.key} onChange={(event) => onPatch({ key: event.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+/, "") })} maxLength={64} className={INPUT} />
            </BuilderField>
            <BuilderField label={t.type} htmlFor={`${question.clientId}-type`}>
              <select id={`${question.clientId}-type`} value={value.question_type} onChange={(event) => {
                const next = event.target.value as QuestionType;
                onPatch({
                  question_type: next,
                  options: next === "single" || next === "multi" ? value.options : [],
                  scoring: null,
                  knockout: null,
                });
              }} className={INPUT}>
                {QUESTION_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
              </select>
            </BuilderField>
            <BuilderField label={t.prompt} htmlFor={`${question.clientId}-prompt`} wide>
              <textarea id={`${question.clientId}-prompt`} value={value.prompt} onChange={(event) => onPatch({ prompt: event.target.value })} maxLength={10_000} rows={2} className={TEXTAREA} />
            </BuilderField>
            <BuilderField label={t.description} htmlFor={`${question.clientId}-description`} wide>
              <input id={`${question.clientId}-description`} value={value.description ?? ""} onChange={(event) => onPatch({ description: event.target.value || null })} maxLength={10_000} className={INPUT} />
            </BuilderField>
          </div>
          <label className="mt-3 flex min-h-9 cursor-pointer items-center gap-2 text-sm">
            <input type="checkbox" checked={value.required} onChange={(event) => onPatch({ required: event.target.checked })} className="h-4 w-4 accent-primary" /> {t.required}
          </label>

          {supportsOptions ? (
            <BuilderField label={t.options} htmlFor={`${question.clientId}-options`} wide>
              <textarea id={`${question.clientId}-options`} value={optionsText(value)} onChange={(event) => onPatch({ options: parseOptions(event.target.value) })} rows={4} className={cn(TEXTAREA, "font-mono")} />
            </BuilderField>
          ) : null}

          <fieldset className="mt-4 rounded-lg border border-border p-3">
            <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-text-muted">{t.validation}</legend>
            <div className="grid gap-3 sm:grid-cols-2">
              {textType ? <>
                <SmallNumber label={t.minLength} value={value.validation?.min_length} onChange={(raw) => setValidation("min_length", raw)} />
                <SmallNumber label={t.maxLength} value={value.validation?.max_length} onChange={(raw) => setValidation("max_length", raw)} />
              </> : null}
              {value.question_type === "number" ? <>
                <SmallNumber label={t.minValue} value={value.validation?.min_value} onChange={(raw) => setValidation("min_value", raw)} />
                <SmallNumber label={t.maxValue} value={value.validation?.max_value} onChange={(raw) => setValidation("max_value", raw)} />
              </> : null}
              {value.question_type === "file" ? <>
                <label className="text-xs text-text-muted">{t.fileExtensions}<input value={Array.isArray(value.validation?.allowed_file_extensions) ? value.validation.allowed_file_extensions.join(", ") : ""} onChange={(event) => setValidation("allowed_file_extensions", event.target.value, (raw) => raw.split(",").map((item) => item.trim().toLowerCase().replace(/^\./, "")).filter(Boolean))} placeholder="pdf, docx" className={cn(INPUT, "mt-1")} /></label>
                <SmallNumber label={t.fileSize} value={typeof value.validation?.max_file_size_bytes === "number" ? value.validation.max_file_size_bytes / 1_048_576 : undefined} onChange={(raw) => setValidation("max_file_size_bytes", raw, (input) => { const parsed = finiteNumber(input); return parsed === undefined ? undefined : Math.round(parsed * 1_048_576); })} />
              </> : null}
              {!textType && value.question_type !== "number" && value.question_type !== "file" ? <p className="text-xs text-text-muted">—</p> : null}
            </div>
          </fieldset>

          <fieldset className="mt-4 rounded-lg border border-border p-3">
            <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-text-muted">{t.branching}</legend>
            <label className="flex min-h-9 cursor-pointer items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={!!condition}
                disabled={previousQuestions.length === 0}
                onChange={(event) => {
                  const source = previousQuestions.at(-1);
                  const operator = source ? OPERATORS_BY_TYPE[source.question_type][0] : "answered";
                  onPatch({
                    condition:
                      event.target.checked && source
                        ? {
                            mode: "all",
                            rules: [
                              {
                                question_key: source.key,
                                operator,
                                value: defaultRuleValue(source, operator),
                              },
                            ],
                          }
                        : null,
                  });
                }}
                className="h-4 w-4 accent-primary"
              /> {t.showWhen}
            </label>
            {condition ? (
              <div className="mt-2 space-y-3">
                <div className="flex flex-wrap items-end justify-between gap-3">
                  <label className="min-w-52 text-xs text-text-muted">
                    {t.matchRules}
                    <select
                      value={conditionMode}
                      onChange={(event) =>
                        onPatch({
                          condition: {
                            mode: event.target.value as "all" | "any",
                            rules: visibilityRules,
                          },
                        })
                      }
                      className={cn(INPUT, "mt-1")}
                    >
                      <option value="all">{t.allRules}</option>
                      <option value="any">{t.anyRule}</option>
                    </select>
                  </label>
                  <button
                    type="button"
                    onClick={addVisibilityRule}
                    disabled={visibilityRules.length >= 20}
                    className="flex min-h-10 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                  >
                    <Plus className="h-4 w-4" /> {t.addRule}
                  </button>
                </div>
                {visibilityRules.map((rule, ruleIndex) => {
                  const source = previousQuestions.find(
                    (item) => item.key === rule.question_key,
                  );
                  const operators = source
                    ? OPERATORS_BY_TYPE[source.question_type]
                    : [];
                  return (
                    <div
                      key={`${rule.question_key}-${ruleIndex}`}
                      className="grid gap-3 rounded-lg border border-border bg-bg p-3 sm:grid-cols-[1fr_1fr_1fr_auto]"
                    >
                      <label className="text-xs text-text-muted">
                        {t.sourceQuestion}
                        <select
                          value={rule.question_key}
                          onChange={(event) => {
                            const nextSource = previousQuestions.find(
                              (item) => item.key === event.target.value,
                            );
                            if (!nextSource) return;
                            const nextOperators = OPERATORS_BY_TYPE[nextSource.question_type];
                            const operator = nextOperators.includes(rule.operator)
                              ? rule.operator
                              : nextOperators[0];
                            patchVisibilityRule(ruleIndex, {
                              question_key: nextSource.key,
                              operator,
                              value: ruleValueIsCompatible(nextSource, operator, rule.value)
                                ? rule.value
                                : defaultRuleValue(nextSource, operator),
                            });
                          }}
                          className={cn(INPUT, "mt-1")}
                        >
                          {previousQuestions.map((item) => (
                            <option key={item.key} value={item.key}>{item.key}</option>
                          ))}
                        </select>
                      </label>
                      <label className="text-xs text-text-muted">
                        {t.operator}
                        <select
                          value={rule.operator}
                          onChange={(event) => {
                            if (!source) return;
                            const operator = event.target.value as ConditionOperator;
                            patchVisibilityRule(ruleIndex, {
                              ...rule,
                              operator,
                              value: defaultRuleValue(source, operator),
                            });
                          }}
                          className={cn(INPUT, "mt-1")}
                        >
                          {!operators.includes(rule.operator) ? (
                            <option value={rule.operator} disabled>{rule.operator}</option>
                          ) : null}
                          {operators.map((operator) => (
                            <option key={operator} value={operator}>{operator}</option>
                          ))}
                        </select>
                      </label>
                      {source ? (
                        <RuleValueEditor
                          label={t.expected}
                          question={source}
                          operator={rule.operator}
                          value={rule.value}
                          onChange={(next) =>
                            patchVisibilityRule(ruleIndex, { ...rule, value: next })
                          }
                        />
                      ) : <span />}
                      <button
                        type="button"
                        aria-label={t.removeRule}
                        title={t.removeRule}
                        onClick={() => removeVisibilityRule(ruleIndex)}
                        disabled={visibilityRules.length <= 1}
                        className="mt-5 flex h-10 w-10 items-center justify-center rounded-lg border border-border text-danger hover:bg-danger/10 focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  );
                })}
              </div>
            ) : null}
          </fieldset>

          <fieldset className="mt-4 rounded-lg border border-border p-3">
            <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-text-muted">{t.scoring}</legend>
            <label className="flex min-h-9 cursor-pointer items-center gap-2 text-sm">
              <input type="checkbox" checked={!!scoring} disabled={value.question_type === "file"} onChange={(event) => onPatch({ scoring: event.target.checked ? defaultScoring(value.question_type, value.options) : null, max_score: event.target.checked ? Math.max(Number(value.max_score), 1) : value.max_score })} className="h-4 w-4 accent-primary" /> {t.scored}
            </label>
            <div className="mt-2 grid gap-3 sm:grid-cols-2">
              <SmallNumber label={t.maxScore} value={value.max_score} onChange={(raw) => onPatch({ max_score: finiteNumber(raw) ?? 0 })} />
              {scoring && value.question_type === "boolean" ? <label className="text-xs text-text-muted">{t.correct}<select value={String(scoring.correct ?? true)} onChange={(event) => patchScoring({ correct: event.target.value === "true" })} className={cn(INPUT, "mt-1")}><option value="true">true</option><option value="false">false</option></select></label> : null}
              {scoring && value.question_type === "single" ? <label className="text-xs text-text-muted">{t.correct}<select value={typeof scoring.correct === "string" ? scoring.correct : ""} onChange={(event) => patchScoring({ correct: event.target.value })} className={cn(INPUT, "mt-1")}><option value="">—</option>{(value.options ?? []).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label> : null}
              {scoring && value.question_type === "multi" ? <label className="text-xs text-text-muted">{t.correct}<input value={Array.isArray(scoring.correct) ? scoring.correct.join(", ") : ""} onChange={(event) => patchScoring({ correct: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) })} className={cn(INPUT, "mt-1")} /></label> : null}
              {scoring && value.question_type === "number" ? (
                <div className="space-y-3 sm:col-span-2">
                  <fieldset>
                    <legend className="text-xs text-text-muted">{t.scoring}</legend>
                    <div className="mt-1 grid gap-2 sm:grid-cols-2">
                      <label className="flex min-h-10 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm">
                        <input
                          type="radio"
                          name={`${question.clientId}-number-scoring-mode`}
                          checked={scoring.correct !== null && scoring.correct !== undefined}
                          onChange={() => patchScoring({ correct: 0, tolerance: 0, min_value: null, max_value: null })}
                          className="accent-primary"
                        />
                        {t.exactMode}
                      </label>
                      <label className="flex min-h-10 items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm">
                        <input
                          type="radio"
                          name={`${question.clientId}-number-scoring-mode`}
                          checked={scoring.correct === null || scoring.correct === undefined}
                          onChange={() => patchScoring({ correct: null, tolerance: null, min_value: 0, max_value: 1 })}
                          className="accent-primary"
                        />
                        {t.rangeMode}
                      </label>
                    </div>
                  </fieldset>
                  {scoring.correct !== null && scoring.correct !== undefined ? (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <SmallNumber label={t.correct} value={scoring.correct as string | number | undefined} onChange={(raw) => patchScoring({ correct: finiteNumber(raw) })} />
                      <SmallNumber label={t.tolerance} value={scoring.tolerance} onChange={(raw) => patchScoring({ tolerance: finiteNumber(raw) })} />
                    </div>
                  ) : (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <SmallNumber label={t.minValue} value={scoring.min_value} onChange={(raw) => patchScoring({ min_value: finiteNumber(raw) })} />
                      <SmallNumber label={t.maxValue} value={scoring.max_value} onChange={(raw) => patchScoring({ max_value: finiteNumber(raw) })} />
                    </div>
                  )}
                </div>
              ) : null}
              {scoring && textType ? <label className="text-xs text-text-muted sm:col-span-2">{t.accepted}<input value={(scoring.accepted_values ?? []).join(", ")} onChange={(event) => patchScoring({ accepted_values: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) })} className={cn(INPUT, "mt-1")} /></label> : null}
            </div>
            {scoring && value.question_type === "multi" ? <label className="mt-2 flex min-h-8 items-center gap-2 text-sm"><input type="checkbox" checked={scoring.partial_credit} onChange={(event) => patchScoring({ partial_credit: event.target.checked })} className="accent-primary" /> {t.partial}</label> : null}
            {scoring && textType ? <label className="mt-2 flex min-h-8 items-center gap-2 text-sm"><input type="checkbox" checked={scoring.case_sensitive} onChange={(event) => patchScoring({ case_sensitive: event.target.checked })} className="accent-primary" /> {t.caseSensitive}</label> : null}
          </fieldset>

          <fieldset className="mt-4 rounded-lg border border-border p-3">
            <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-text-muted">{t.knockout}</legend>
            <label className="flex min-h-9 cursor-pointer items-center gap-2 text-sm"><input type="checkbox" checked={!!knockout} onChange={(event) => {
              const operator = knockoutOperators[0];
              onPatch({
                knockout: event.target.checked
                  ? { operator, value: defaultRuleValue(value, operator), reason: null }
                  : null,
              });
            }} className="h-4 w-4 accent-primary" /> {t.knockoutEnabled}</label>
            {knockout ? <div className="mt-2 grid gap-3 sm:grid-cols-3">
              <label className="text-xs text-text-muted">{t.operator}<select value={knockout.operator} onChange={(event) => {
                const operator = event.target.value as Knockout["operator"];
                patchKnockout({ operator, value: defaultRuleValue(value, operator) });
              }} className={cn(INPUT, "mt-1")}>
                {!knockoutOperators.includes(knockout.operator) ? <option value={knockout.operator} disabled>{knockout.operator}</option> : null}
                {knockoutOperators.map((operator) => <option key={operator} value={operator}>{operator}</option>)}
              </select></label>
              <RuleValueEditor
                label={t.expected}
                question={value}
                operator={knockout.operator}
                value={knockout.value}
                onChange={(next) => patchKnockout({ value: next })}
              />
              <label className="text-xs text-text-muted">{t.reason}<input value={knockout.reason ?? ""} onChange={(event) => patchKnockout({ reason: event.target.value || null })} maxLength={2_000} className={cn(INPUT, "mt-1")} /></label>
            </div> : null}
          </fieldset>
        </div>
      ) : null}
    </article>
  );
}

const INPUT = "min-h-10 w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30";
const TEXTAREA = "w-full rounded-lg border border-input bg-bg px-3 py-2 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/30";

function BuilderField({ label, hint, htmlFor, wide, children }: { label: string; hint?: string; htmlFor: string; wide?: boolean; children: React.ReactNode }) {
  return <div className={wide ? "sm:col-span-2" : undefined}><label htmlFor={htmlFor} className="block text-xs font-medium text-text-muted">{label}</label>{hint ? <p className="mt-0.5 text-xs text-text-faint">{hint}</p> : null}<div className="mt-1">{children}</div></div>;
}

function SmallNumber({ label, value, onChange }: { label: string; value: unknown; onChange: (raw: string) => void }) {
  return <label className="text-xs text-text-muted">{label}<input type="number" step="any" value={typeof value === "number" || typeof value === "string" ? value : ""} onChange={(event) => onChange(event.target.value)} className={cn(INPUT, "mt-1")} /></label>;
}

function RuleValueEditor({
  label,
  question,
  operator,
  value,
  onChange,
}: {
  label: string;
  question: PreviousQuestion;
  operator: ConditionOperator;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  if (!operatorNeedsValue(operator)) return null;
  if (question.question_type === "boolean") {
    return (
      <label className="text-xs text-text-muted">
        {label}
        <select
          value={String(value ?? true)}
          onChange={(event) => onChange(event.target.value === "true")}
          className={cn(INPUT, "mt-1")}
        >
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </label>
    );
  }
  if (question.question_type === "single" && operator === "in") {
    const selected = new Set(Array.isArray(value) ? value.map(String) : []);
    return (
      <label className="text-xs text-text-muted">
        {label}
        <select
          multiple
          value={[...selected]}
          onChange={(event) =>
            onChange(Array.from(event.currentTarget.selectedOptions, (option) => option.value))
          }
          className={cn(INPUT, "mt-1 min-h-24")}
        >
          {(question.options ?? []).map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </label>
    );
  }
  if (question.question_type === "single" || question.question_type === "multi") {
    return (
      <label className="text-xs text-text-muted">
        {label}
        <select
          value={typeof value === "string" ? value : ""}
          onChange={(event) => onChange(event.target.value)}
          className={cn(INPUT, "mt-1")}
        >
          <option value="">—</option>
          {(question.options ?? []).map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </label>
    );
  }
  if (question.question_type === "number") {
    return (
      <SmallNumber
        label={label}
        value={value}
        onChange={(raw) => onChange(finiteNumber(raw))}
      />
    );
  }
  if (operator === "in") {
    return (
      <label className="text-xs text-text-muted">
        {label}
        <input
          value={Array.isArray(value) ? value.join(", ") : ""}
          onChange={(event) =>
            onChange(
              event.target.value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean),
            )
          }
          className={cn(INPUT, "mt-1")}
        />
      </label>
    );
  }
  return (
    <label className="text-xs text-text-muted">
      {label}
      <input
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value)}
        className={cn(INPUT, "mt-1")}
      />
    </label>
  );
}

function IconButton({ label, disabled, destructive, onClick, children }: { label: string; disabled?: boolean; destructive?: boolean; onClick: () => void; children: React.ReactElement<{ className?: string }> }) {
  return <button type="button" aria-label={label} title={label} disabled={disabled} onClick={onClick} className={cn("flex h-8 w-8 items-center justify-center rounded-md text-text-muted hover:bg-bg-hover hover:text-text focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-30", destructive && "hover:bg-destructive/10 hover:text-destructive")}>{children}</button>;
}

function defaultScoring(
  type: QuestionType,
  options: QuestionInput["options"] = [],
): Scoring {
  const first = options?.[0]?.value;
  const correct =
    type === "boolean"
      ? true
      : type === "single"
        ? first
        : type === "multi"
          ? first
            ? [first]
            : []
          : type === "number"
            ? 0
            : undefined;
  return {
    accepted_values: [],
    correct,
    tolerance: 0,
    min_value: null,
    max_value: null,
    partial_credit: false,
    case_sensitive: false,
  };
}

function normalizeQuestion(question: QuestionInput): QuestionInput {
  const supportsOptions = question.question_type === "single" || question.question_type === "multi";
  return {
    ...question,
    key: question.key.trim(),
    prompt: question.prompt.trim(),
    description: question.description?.trim() || null,
    options: supportsOptions ? question.options ?? [] : [],
    validation: question.validation ?? {},
  };
}

function move<T>(items: T[], index: number, direction: -1 | 1): T[] {
  const target = index + direction;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

function toggleSet(current: Set<string>, value: string): Set<string> {
  const next = new Set(current);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}
