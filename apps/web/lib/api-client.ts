import "server-only";

import { headers as requestHeaders } from "next/headers";

import { INTERNAL_ACCESS_HEADER } from "@/lib/auth-session";
import { apiProblemMessage } from "@/lib/api-problem";
import type {
  ApiMethod,
  ApiOperation,
  ApiOperationRequestBody,
  ApiOperationResponse,
  ApiPath,
  ApiResponseSchema,
  ApiSerialized,
} from "@/lib/generated/contracts";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

/** Generated pagination envelope with a caller-selected generated item type. */
export type CursorPage<T> = Omit<
  ApiResponseSchema<"ApplicationPage">,
  "items"
> & { items: T[] };

function isCursorPage(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const page = value as { items?: unknown; next_cursor?: unknown };
  return (
    Array.isArray(page.items) &&
    (page.next_cursor === null || typeof page.next_cursor === "string")
  );
}

type PathForMethod<Method extends ApiMethod> = {
  [Path in ApiPath]: ApiOperation<Path, Method> extends never ? never : Path;
}[ApiPath];

export type ApiGetContract<Path extends PathForMethod<"get">> = {
  /** OpenAPI template used only for compile-time operation selection. */
  template: Path;
  /** Concrete URL, including substituted path values and an optional query. */
  path: string;
};

/** @local-only HTTP-method subset supported by the JSON operation helper. */
type JsonMutationMethod = "post" | "put" | "patch" | "delete";

type ApiJsonMutationBase<
  Path extends PathForMethod<Method>,
  Method extends JsonMutationMethod,
> = {
  template: Path;
  path: string;
  method: Method;
  headers?: HeadersInit;
};

type ApiJsonMutationBody<
  Path extends PathForMethod<Method>,
  Method extends JsonMutationMethod,
> = ApiOperationRequestBody<Path, Method> extends never
  ? { body?: never }
  : { body: ApiOperationRequestBody<Path, Method> };

export type ApiJsonMutation<
  Path extends PathForMethod<Method>,
  Method extends JsonMutationMethod,
> = ApiJsonMutationBase<Path, Method> & ApiJsonMutationBody<Path, Method>;

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const incomingHeaders = await requestHeaders();
  const token = incomingHeaders.get(INTERNAL_ACCESS_HEADER);
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  // Default to JSON for non-multipart bodies. FormData must NOT have an
  // explicit Content-Type — fetch generates the multipart boundary itself,
  // and forcing application/json strips the body to an empty multipart blob
  // (which the API rejects with `field "file" missing`).
  const isFormData =
    typeof FormData !== "undefined" && init.body instanceof FormData;
  if (init.body && !isFormData && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers, cache: "no-store" });
  if (res.status === 401) {
    // API JWT has expired or is invalid. The NextAuth session cookie may still
    // be live (different lifetime), so middleware won't catch this — bounce to
    // /login directly. The ?expired=1 flag tells the login page to render the
    // form instead of auto-redirecting back to /dashboard (which would loop).
    const { redirect } = await import("next/navigation");
    redirect("/login?expired=1");
  }
  return res;
}

export async function getServerAccessToken(): Promise<string | null> {
  const incomingHeaders = await requestHeaders();
  return incomingHeaders.get(INTERNAL_ACCESS_HEADER);
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await apiFetch(path);
  if (!res.ok) throw new Error(await apiProblemMessage(res));
  return res.json() as Promise<T>;
}

/**
 * Generated-operation GET. Prefer this for new server code: the template
 * selects the OpenAPI operation while `path` carries concrete identifiers.
 */
export async function apiGetOperation<Path extends PathForMethod<"get">>(
  contract: ApiGetContract<Path>,
): Promise<ApiSerialized<ApiOperationResponse<Path, "get">>> {
  void contract.template;
  return apiGet<ApiSerialized<ApiOperationResponse<Path, "get">>>(contract.path);
}

/** Generated-operation JSON mutation with request and success-response typing. */
export async function apiJsonOperation<
  Method extends JsonMutationMethod,
  Path extends PathForMethod<Method>,
>(
  contract: ApiJsonMutation<Path, Method>,
): Promise<ApiSerialized<ApiOperationResponse<Path, Method>>> {
  void contract.template;
  const hasBody = "body" in contract && contract.body !== undefined;
  const response = await apiFetch(contract.path, {
    method: contract.method.toUpperCase(),
    headers: contract.headers,
    body: hasBody ? JSON.stringify(contract.body) : undefined,
  });
  if (!response.ok) {
    throw new Error(await apiProblemMessage(response));
  }
  if (response.status === 204) {
    return undefined as ApiSerialized<ApiOperationResponse<Path, Method>>;
  }
  return response.json() as Promise<
    ApiSerialized<ApiOperationResponse<Path, Method>>
  >;
}

function withCollectionPage(path: string, cursor: string | null): string {
  const [pathname, rawQuery = ""] = path.split("?", 2);
  const params = new URLSearchParams(rawQuery);
  if (!params.has("limit")) params.set("limit", "100");
  if (cursor) params.set("cursor", cursor);
  else params.delete("cursor");
  return `${pathname}?${params.toString()}`;
}

/** Read an entire cursor collection while rejecting replayed pagination tokens. */
export async function apiGetAll<T>(path: string): Promise<T[]> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  for (let pageNumber = 0; pageNumber < 1_000; pageNumber += 1) {
    const page: CursorPage<T> = await apiGet<CursorPage<T>>(
      withCollectionPage(path, cursor),
    );
    if (!isCursorPage(page)) {
      throw new Error("API returned an invalid collection page");
    }
    items.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) return items;
    if (seen.has(cursor)) throw new Error("API returned a repeated collection cursor");
    seen.add(cursor);
  }
  throw new Error("API collection exceeded the safe page traversal limit");
}

/** Read every page while retaining metadata from the first response. */
export async function apiGetAllPage<
  T,
  P extends CursorPage<T>,
>(path: string): Promise<P> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let firstPage: P | null = null;
  for (let pageNumber = 0; pageNumber < 1_000; pageNumber += 1) {
    const page: P = await apiGet<P>(withCollectionPage(path, cursor));
    firstPage ??= page;
    items.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) {
      return { ...firstPage, items, next_cursor: null };
    }
    if (seen.has(cursor)) throw new Error("API returned a repeated collection cursor");
    seen.add(cursor);
  }
  throw new Error("API collection exceeded the safe page traversal limit");
}
