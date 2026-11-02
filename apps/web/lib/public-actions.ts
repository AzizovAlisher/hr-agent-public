"use server";

/**
 * Server actions for the PUBLIC apply page (no auth required).
 * Lives outside lib/actions.ts on purpose so it can never accidentally
 * leak the bearer token of a signed-in admin browsing the page in another tab.
 */

import type { ApiResponseSchema } from "@/lib/generated/contracts";
import { apiProblemMessage } from "@/lib/api-problem";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

export type PublicPosition = ApiResponseSchema<"PublicPositionOut">;

export async function getPublicPosition(
  positionId: string,
): Promise<PublicPosition | null> {
  const res = await fetch(
    `${API_BASE_URL}/public/positions/${positionId}`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  return (await res.json()) as PublicPosition;
}

/** @ui-only Serializable result returned by the public apply server action. */
export type ApplyResult =
  | { ok: true }
  | { ok: false; error: string };

export type PublicReferral = ApiResponseSchema<"PublicReferralOut">;

export async function resolvePublicReferral(
  token: string,
): Promise<PublicReferral | null> {
  const res = await fetch(
    `${API_BASE_URL}/public/referrals/${encodeURIComponent(token)}`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  return (await res.json()) as PublicReferral;
}

export async function submitApplication(
  positionId: string,
  formData: FormData,
): Promise<ApplyResult> {
  // Forward the multipart form straight to the API. Drop any client-side
  // junk; FastAPI validates the rest.
  const proxy = new FormData();
  for (const key of [
    "name",
    "phone",
    "email",
    "source",
    "referral_token",
    "raw_text",
    "privacy_notice_version",
    "privacy_acknowledged",
  ]) {
    const v = formData.get(key);
    if (typeof v === "string" && v.length > 0) proxy.set(key, v);
  }
  const file = formData.get("file");
  if (file instanceof File && file.size > 0) {
    proxy.set("file", file);
  }
  const requestedIdempotencyKey = formData.get("idempotency_key");
  const idempotencyKey =
    typeof requestedIdempotencyKey === "string" && requestedIdempotencyKey.length > 0
      ? requestedIdempotencyKey.slice(0, 160)
      : crypto.randomUUID();

  const res = await fetch(
    `${API_BASE_URL}/public/positions/${positionId}/apply`,
    {
      method: "POST",
      body: proxy,
      headers: { "Idempotency-Key": idempotencyKey },
      cache: "no-store",
    },
  );
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  return { ok: true };
}
