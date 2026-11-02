import "server-only";

import { headers as requestHeaders } from "next/headers";

/** @local-only Sanitized RFC 9457 failure safe to return from a server action. */
export type ApiProblem = {
  status: number;
  code: string;
  requestId: string | null;
};

const CODE_PATTERN = /^[a-z0-9][a-z0-9_.-]{0,95}$/;
const REQUEST_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const COPY = {
  en: {
    invalid_request: "The request is invalid.",
    validation_failed: "Check the entered values and try again.",
    unauthorized: "Your session is no longer valid. Sign in again.",
    forbidden: "You do not have permission to perform this action.",
    not_found: "The requested record was not found.",
    gone: "This link or record is no longer available.",
    conflict: "The record changed. Refresh and try again.",
    idempotency_conflict: "This request key was already used for different data.",
    limit_exceeded: "This request exceeds the technical limit.",
    entitlement_exceeded: "Your organization has reached its contract limit.",
    unsupported_media_type: "This file type is not supported.",
    rate_limited: "Too many requests. Wait briefly and try again.",
    service_unavailable: "The service is temporarily unavailable.",
    internal_error: "The request could not be completed.",
    fallback: "The request could not be completed.",
    reference: "Reference",
  },
  ru: {
    invalid_request: "Запрос составлен некорректно.",
    validation_failed: "Проверьте введённые данные и повторите попытку.",
    unauthorized: "Сессия больше недействительна. Войдите снова.",
    forbidden: "У вас нет прав для этого действия.",
    not_found: "Запрошенная запись не найдена.",
    gone: "Ссылка или запись больше недоступна.",
    conflict: "Запись изменилась. Обновите страницу и повторите попытку.",
    idempotency_conflict: "Этот ключ запроса уже использован для других данных.",
    limit_exceeded: "Запрос превышает технический лимит.",
    entitlement_exceeded: "Организация достигла договорного лимита.",
    unsupported_media_type: "Этот тип файла не поддерживается.",
    rate_limited: "Слишком много запросов. Немного подождите и повторите попытку.",
    service_unavailable: "Сервис временно недоступен.",
    internal_error: "Не удалось выполнить запрос.",
    fallback: "Не удалось выполнить запрос.",
    reference: "Код обращения",
  },
} as const;

function fallbackCode(status: number): string {
  if (status === 400) return "invalid_request";
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 410) return "gone";
  if (status === 413) return "limit_exceeded";
  if (status === 415) return "unsupported_media_type";
  if (status === 422) return "validation_failed";
  if (status === 429) return "rate_limited";
  if (status === 503) return "service_unavailable";
  if (status >= 500) return "internal_error";
  return "invalid_request";
}

export async function readApiProblem(response: Response): Promise<ApiProblem> {
  let code = fallbackCode(response.status);
  let requestId: string | null = null;
  const headerRequestId = response.headers.get("x-request-id");
  if (headerRequestId && REQUEST_ID_PATTERN.test(headerRequestId)) {
    requestId = headerRequestId;
  }
  try {
    const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
    if (contentType.includes("json")) {
      const value: unknown = JSON.parse(await response.text());
      if (value && typeof value === "object" && !Array.isArray(value)) {
        const bodyCode = Reflect.get(value, "code");
        const bodyRequestId = Reflect.get(value, "request_id");
        if (typeof bodyCode === "string" && CODE_PATTERN.test(bodyCode)) {
          code = bodyCode;
        }
        if (
          typeof bodyRequestId === "string" &&
          REQUEST_ID_PATTERN.test(bodyRequestId)
        ) {
          requestId = bodyRequestId;
        }
      }
    }
  } catch {
    // Malformed or legacy error bodies never reach product copy.
  }
  return { status: response.status, code, requestId };
}

export async function apiProblemMessage(response: Response): Promise<string> {
  const problem = await readApiProblem(response);
  const acceptLanguage = (await requestHeaders()).get("accept-language") ?? "";
  const locale = /(^|,)\s*ru(?:-|,|;|$)/i.test(acceptLanguage) ? "ru" : "en";
  const copy = COPY[locale];
  const message = problem.code in copy
    ? copy[problem.code as keyof typeof copy]
    : copy.fallback;
  return problem.requestId
    ? `${message} ${copy.reference}: ${problem.requestId}.`
    : message;
}

export async function apiUnavailableMessage(): Promise<string> {
  return apiProblemMessage(new Response(null, { status: 503 }));
}
