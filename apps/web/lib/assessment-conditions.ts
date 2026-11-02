import type { components } from "@/lib/generated/api";

type PublicQuestion = components["schemas"]["PublicAssessmentQuestion"];
type QuestionType = PublicQuestion["question_type"];

/** @ui-only Autosaved answer state keyed by assessment question. */
export type AssessmentAnswers = Record<string, unknown>;

function isAnswered(value: unknown): boolean {
  if (value == null) return false;
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function numeric(value: unknown): number | null {
  if (typeof value === "boolean" || value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function conditionMatches(
  condition: Record<string, unknown>,
  answersByKey: AssessmentAnswers,
  questionTypesByKey: Record<string, QuestionType> = {},
): boolean {
  if (Array.isArray(condition.rules)) {
    if (condition.rules.length === 0) return false;
    const results = condition.rules.map((rule) =>
      rule && typeof rule === "object" && !Array.isArray(rule)
        ? conditionMatches(
            rule as Record<string, unknown>,
            answersByKey,
            questionTypesByKey,
          )
        : false,
    );
    return condition.mode === "any"
      ? results.some(Boolean)
      : results.every(Boolean);
  }
  const key = typeof condition.question_key === "string" ? condition.question_key : "";
  if (!key) return false;

  const operator = typeof condition.operator === "string" ? condition.operator : "equals";
  const hasActual = Object.prototype.hasOwnProperty.call(answersByKey, key);
  const actual = answersByKey[key];
  const expected = condition.value;
  const sourceType = questionTypesByKey[key];

  if (operator === "answered") return hasActual && isAnswered(actual);
  if (operator === "not_answered") return !hasActual || !isAnswered(actual);
  if (!hasActual) return false;

  if (operator === "equals" || operator === "not_equals") {
    // Decimal values in JSON-backed assessment conditions are serialized as
    // strings by the API (for example, `"2.0"`), while an HTML number input
    // yields a number. Match the backend's Decimal comparison for numeric
    // source questions without weakening exact equality for text questions.
    const matched =
      sourceType === "number"
        ? numeric(actual) !== null && numeric(actual) === numeric(expected)
        : Object.is(actual, expected);
    return operator === "equals" ? matched : !matched;
  }
  if (operator === "contains") {
    return Array.isArray(actual)
      ? actual.some((item) => Object.is(item, expected))
      : typeof actual === "string" && typeof expected === "string"
        ? actual.includes(expected)
        : false;
  }
  if (operator === "not_contains") {
    return Array.isArray(actual)
      ? !actual.some((item) => Object.is(item, expected))
      : typeof actual === "string" && typeof expected === "string"
        ? !actual.includes(expected)
        : false;
  }
  if (operator === "in") {
    return Array.isArray(expected) && expected.some((item) => Object.is(item, actual));
  }

  const left = numeric(actual);
  const right = numeric(expected);
  if (left == null || right == null) return false;
  if (operator === "gt") return left > right;
  if (operator === "gte") return left >= right;
  if (operator === "lt") return left < right;
  if (operator === "lte") return left <= right;
  return false;
}

export function visibleQuestions(
  questions: PublicQuestion[],
  answersByQuestionId: AssessmentAnswers,
): PublicQuestion[] {
  const answersByKey: AssessmentAnswers = {};
  const questionTypesByKey: Record<string, QuestionType> = {};
  const visible: PublicQuestion[] = [];

  for (const question of questions) {
    const condition = question.condition ?? {};
    const isVisible =
      Object.keys(condition).length === 0 ||
      conditionMatches(condition, answersByKey, questionTypesByKey);
    if (isVisible) {
      visible.push(question);
      const value = answersByQuestionId[question.id];
      if (isAnswered(value)) answersByKey[question.key] = value;
    }
    questionTypesByKey[question.key] = question.question_type;
  }
  return visible;
}

export function answerIsPresent(value: unknown): boolean {
  return isAnswered(value);
}
