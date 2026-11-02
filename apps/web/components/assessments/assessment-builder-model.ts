import type { components } from "@/lib/generated/api";

export type AssessmentTemplate = components["schemas"]["AssessmentTemplateOut"];
export type AssessmentVersion = components["schemas"]["AssessmentVersionOut"];
export type AssessmentQuestion = components["schemas"]["AssessmentQuestionOut"];
type GeneratedVersionInput = components["schemas"]["AssessmentVersionCreate"];
type GeneratedSectionInput = components["schemas"]["AssessmentSectionCreate"];
type GeneratedQuestionInput = components["schemas"]["AssessmentQuestionCreate"];
export type ConditionRule = components["schemas"]["AssessmentCondition"];
export type ConditionGroup = {
  mode: "all" | "any";
  rules: ConditionRule[];
};
export type Condition = ConditionRule | ConditionGroup;
export type QuestionInput = Omit<GeneratedQuestionInput, "condition"> & {
  condition?: Condition | null;
};
export type SectionInput = Omit<GeneratedSectionInput, "questions"> & {
  questions: QuestionInput[];
};
export type VersionInput = Omit<GeneratedVersionInput, "sections"> & {
  sections: SectionInput[];
};
export type QuestionType = QuestionInput["question_type"];
export type ConditionOperator = ConditionRule["operator"];
export type Scoring = NonNullable<QuestionInput["scoring"]>;
export type Knockout = NonNullable<QuestionInput["knockout"]>;

export type DraftQuestion = { clientId: string; value: QuestionInput };
export type DraftSection = {
  clientId: string;
  title: string;
  description: string;
  questions: DraftQuestion[];
};

export type BuilderState = {
  title: string;
  instructions: string;
  passing: number;
  timeLimit: string;
  sections: DraftSection[];
  baseVersion: { id: string; number: number } | null;
};

export type PreviousQuestion = Pick<QuestionInput, "key" | "question_type" | "options">;

export const QUESTION_TYPES: QuestionType[] = [
  "boolean",
  "single",
  "multi",
  "number",
  "short",
  "long",
  "url",
  "file",
];

export const OPERATORS_BY_TYPE: Record<QuestionType, readonly ConditionOperator[]> = {
  boolean: ["equals", "not_equals", "answered", "not_answered"],
  single: ["equals", "not_equals", "in", "answered", "not_answered"],
  multi: ["contains", "not_contains", "answered", "not_answered"],
  number: [
    "equals",
    "not_equals",
    "gt",
    "gte",
    "lt",
    "lte",
    "answered",
    "not_answered",
  ],
  short: [
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "in",
    "answered",
    "not_answered",
  ],
  long: [
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "in",
    "answered",
    "not_answered",
  ],
  url: [
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "in",
    "answered",
    "not_answered",
  ],
  file: ["answered", "not_answered"],
};

const QUESTION_TYPE_SET = new Set<QuestionType>(QUESTION_TYPES);
const VALUELESS_OPERATORS = new Set<ConditionOperator>([
  "answered",
  "not_answered",
]);
const CONDITION_OPERATOR_SET = new Set<ConditionOperator>(
  Object.values(OPERATORS_BY_TYPE).flat(),
);
const DRAFT_SCHEMA_VERSION = 1;

export function operatorNeedsValue(operator: ConditionOperator): boolean {
  return !VALUELESS_OPERATORS.has(operator);
}

export function isConditionGroup(condition: Condition): condition is ConditionGroup {
  return "rules" in condition;
}

export function conditionRules(condition: Condition): ConditionRule[] {
  return isConditionGroup(condition) ? condition.rules : [condition];
}

export function defaultRuleValue(
  question: PreviousQuestion,
  operator: ConditionOperator,
): unknown {
  if (!operatorNeedsValue(operator)) return null;
  const firstOption = question.options?.[0]?.value;
  if (question.question_type === "boolean") return true;
  if (question.question_type === "single") {
    return operator === "in" ? (firstOption ? [firstOption] : []) : (firstOption ?? "");
  }
  if (question.question_type === "multi") return firstOption ?? "";
  if (question.question_type === "number") return 0;
  if (operator === "in") return [];
  return "";
}

export function initialQuestion(clientId: string, index: number): DraftQuestion {
  return {
    clientId,
    value: {
      key: `question_${index + 1}`,
      question_type: "short",
      prompt: "",
      description: null,
      required: true,
      options: [],
      validation: {},
      condition: null,
      scoring: null,
      knockout: null,
      max_score: 0,
    },
  };
}

export function initialSection(clientId = "section-0"): DraftSection {
  return {
    clientId,
    title: "",
    description: "",
    questions: [initialQuestion("question-0", 0)],
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function isStoredConditionRule(value: unknown): value is ConditionRule {
  const condition = asRecord(value);
  return (
    typeof condition.question_key === "string" &&
    typeof condition.operator === "string" &&
    CONDITION_OPERATOR_SET.has(condition.operator as ConditionOperator)
  );
}

function isStoredCondition(value: unknown): value is Condition {
  if (isStoredConditionRule(value)) return true;
  const condition = asRecord(value);
  return (
    (condition.mode === "all" || condition.mode === "any") &&
    Array.isArray(condition.rules) &&
    condition.rules.length >= 1 &&
    condition.rules.length <= 20 &&
    condition.rules.every(isStoredConditionRule)
  );
}

function optionalObject<T>(value: unknown): T | null {
  const record = asRecord(value);
  return Object.keys(record).length ? (record as T) : null;
}

function cloneQuestion(question: AssessmentQuestion): QuestionInput {
  const options = (question.options ?? []).flatMap((option) => {
    const record = asRecord(option);
    return typeof record.value === "string" && typeof record.label === "string"
      ? [{ value: record.value, label: record.label }]
      : [];
  });
  return {
    key: question.key,
    question_type: question.question_type,
    prompt: question.prompt,
    description: question.description,
    required: question.required,
    options,
    validation: { ...asRecord(question.validation) },
    condition: optionalObject<Condition>(question.condition),
    scoring: optionalObject<Scoring>(question.scoring),
    knockout: optionalObject<Knockout>(question.knockout),
    max_score: question.max_score,
  };
}

function stateFromVersion(version: AssessmentVersion): BuilderState {
  const sections = [...version.sections]
    .sort((left, right) => left.position - right.position)
    .map((section, sectionIndex) => ({
      clientId: `clone-${version.id}-section-${sectionIndex}`,
      title: section.title,
      description: section.description ?? "",
      questions: [...section.questions]
        .sort((left, right) => left.position - right.position)
        .map((question, questionIndex) => ({
          clientId: `clone-${version.id}-question-${sectionIndex}-${questionIndex}`,
          value: cloneQuestion(question),
        })),
    }));
  return {
    title: version.title,
    instructions: version.instructions ?? "",
    passing: Number(version.passing_percentage),
    timeLimit: version.time_limit_minutes?.toString() ?? "",
    sections,
    baseVersion: { id: version.id, number: version.version },
  };
}

export function stateFromTemplate(template: AssessmentTemplate): BuilderState {
  const latest = [...(template.versions ?? [])].sort(
    (left, right) => right.version - left.version,
  )[0];
  if (latest) return stateFromVersion(latest);
  return {
    title: template.name,
    instructions: "",
    passing: 70,
    timeLimit: "60",
    sections: [initialSection()],
    baseVersion: null,
  };
}

export function stateFromPublishedVersion(version: AssessmentVersion): BuilderState {
  return stateFromVersion(version);
}

function draftPayload(state: BuilderState) {
  return {
    title: state.title,
    instructions: state.instructions,
    passing: state.passing,
    timeLimit: state.timeLimit,
    baseVersion: state.baseVersion,
    sections: state.sections.map((section) => ({
      title: section.title,
      description: section.description,
      questions: section.questions.map((question) => question.value),
    })),
  };
}

export function draftFingerprint(state: BuilderState): string {
  return JSON.stringify(draftPayload(state));
}

export function serializeDraft(
  templateId: string,
  state: BuilderState,
  savedAt: string,
): string {
  return JSON.stringify({
    schemaVersion: DRAFT_SCHEMA_VERSION,
    templateId,
    savedAt,
    ...draftPayload(state),
  });
}

type StoredDraft = ReturnType<typeof draftPayload> & {
  schemaVersion: number;
  templateId: string;
  savedAt: string;
};

function isStoredQuestion(value: unknown): value is QuestionInput {
  const question = asRecord(value);
  const options = question.options;
  const maximum = Number(question.max_score);
  const description = question.description;
  const configurations = [
    question.validation,
    question.condition,
    question.scoring,
    question.knockout,
  ];
  return (
    typeof question.key === "string" && question.key.length <= 64 &&
    typeof question.prompt === "string" && question.prompt.length <= 10_000 &&
    (description === undefined || description === null || typeof description === "string") &&
    typeof question.required === "boolean" &&
    typeof question.question_type === "string" &&
    QUESTION_TYPE_SET.has(question.question_type as QuestionType) &&
    Number.isFinite(maximum) && maximum >= 0 && maximum <= 1_000_000 &&
    (options === undefined ||
      (Array.isArray(options) &&
        options.length <= 100 &&
        options.every((option) => {
          const record = asRecord(option);
          return (
            typeof record.value === "string" &&
            typeof record.label === "string"
          );
        }))) &&
    configurations.every(
      (configuration) =>
        configuration === undefined ||
        configuration === null ||
        (typeof configuration === "object" && !Array.isArray(configuration)),
    ) &&
    (question.condition === undefined ||
      question.condition === null ||
      isStoredCondition(question.condition))
  );
}

function isStoredBaseVersion(value: unknown): value is BuilderState["baseVersion"] {
  if (value === null) return true;
  const base = asRecord(value);
  return (
    typeof base.id === "string" &&
    base.id.length > 0 &&
    Number.isInteger(base.number) &&
    Number(base.number) >= 1
  );
}

function isStoredDraft(value: unknown, templateId: string): value is StoredDraft {
  const draft = asRecord(value);
  if (
    draft.schemaVersion !== DRAFT_SCHEMA_VERSION ||
    draft.templateId !== templateId ||
    typeof draft.savedAt !== "string" ||
    !Number.isFinite(Date.parse(draft.savedAt)) ||
    typeof draft.title !== "string" ||
    typeof draft.instructions !== "string" ||
    typeof draft.passing !== "number" ||
    !Number.isFinite(draft.passing) ||
    draft.passing < 0 ||
    draft.passing > 100 ||
    typeof draft.timeLimit !== "string" ||
    (draft.timeLimit !== "" &&
      (!/^\d+$/.test(draft.timeLimit) ||
        Number(draft.timeLimit) < 1 ||
        Number(draft.timeLimit) > 10_080)) ||
    !isStoredBaseVersion(draft.baseVersion) ||
    !Array.isArray(draft.sections) ||
    draft.sections.length < 1 ||
    draft.sections.length > 50
  ) {
    return false;
  }
  return draft.sections.every((value) => {
    const section = asRecord(value);
    return (
      typeof section.title === "string" &&
      typeof section.description === "string" &&
      Array.isArray(section.questions) &&
      section.questions.length >= 1 &&
      section.questions.length <= 200 &&
      section.questions.every(isStoredQuestion)
    );
  });
}

function runtimeId(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

export function parseDraft(
  raw: string,
  templateId: string,
): { state: BuilderState; savedAt: string } | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isStoredDraft(parsed, templateId)) return null;
    return {
      savedAt: parsed.savedAt,
      state: {
        title: parsed.title,
        instructions: parsed.instructions,
        passing: parsed.passing,
        timeLimit: parsed.timeLimit,
        baseVersion: parsed.baseVersion,
        sections: parsed.sections.map((section) => ({
          clientId: runtimeId("restored-section"),
          title: section.title,
          description: section.description,
          questions: section.questions.map((question) => ({
            clientId: runtimeId("restored-question"),
            value: question,
          })),
        })),
      },
    };
  } catch {
    return null;
  }
}
