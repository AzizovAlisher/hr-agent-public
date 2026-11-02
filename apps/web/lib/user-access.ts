import "server-only";

import { unauthenticatedAuthFetch } from "@/lib/auth-api";
import type { ApiResponseSchema } from "@/lib/generated/contracts";

export type UserAccessPreview = ApiResponseSchema<"UserAccessPreviewOut">;

/** @ui-only Non-enumerating public-token state for an access page. */
export type UserAccessPreviewResult =
  | { status: "ok"; data: UserAccessPreview }
  | { status: "not_found" | "gone" | "unavailable" };

const TOKEN = /^[A-Za-z0-9_-]{32,128}$/;

function isPreview(value: unknown): value is UserAccessPreview {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  return (
    (item.purpose === "invitation" || item.purpose === "password_reset") &&
    typeof item.email === "string" &&
    typeof item.role === "string" &&
    (item.locale === "ru" || item.locale === "en") &&
    typeof item.organization_name === "string" &&
    typeof item.expires_at === "string"
  );
}

export async function previewUserAccess(
  purpose: "invitation" | "password_reset",
  token: string,
): Promise<UserAccessPreviewResult> {
  if (!TOKEN.test(token)) return { status: "not_found" };
  const path =
    purpose === "invitation"
      ? `/auth/invitations/${encodeURIComponent(token)}`
      : `/auth/password-reset/${encodeURIComponent(token)}`;
  let response: Response;
  try {
    response = await unauthenticatedAuthFetch(path, { method: "GET" });
  } catch {
    return { status: "unavailable" };
  }
  if (response.status === 404) return { status: "not_found" };
  if (response.status === 410) return { status: "gone" };
  if (!response.ok) return { status: "unavailable" };
  try {
    const data = (await response.json()) as UserAccessPreview;
    return isPreview(data) ? { status: "ok", data } : { status: "unavailable" };
  } catch {
    return { status: "unavailable" };
  }
}
