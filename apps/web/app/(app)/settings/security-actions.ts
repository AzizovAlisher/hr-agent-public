"use server";

import { revalidatePath } from "next/cache";
import { z } from "zod";

import { signOut } from "@/auth";
import { apiFetch } from "@/lib/api-client";
import { clearApiSessionCookie } from "@/lib/auth-session";

/** @ui-only Serializable form state returned by a Next.js server action. */
export type SecurityActionResult =
  | { ok: true }
  | { ok: false; error: "invalid" | "unauthorized" | "not_found" | "failed" };

const uuidSchema = z.uuid();

export async function revokeSessionAction(
  sessionId: string,
  current: boolean,
): Promise<SecurityActionResult> {
  if (!uuidSchema.safeParse(sessionId).success) return { ok: false, error: "invalid" };
  const response = await apiFetch(`/auth/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    return {
      ok: false,
      error:
        response.status === 401
          ? "unauthorized"
          : response.status === 404
            ? "not_found"
            : "failed",
    };
  }
  if (current) {
    await clearApiSessionCookie();
    await signOut({ redirectTo: "/login" });
  }
  revalidatePath("/settings");
  return { ok: true };
}

export async function disableMfaAction(
  currentPassword: string,
  method: "code" | "recovery",
  verifier: string,
): Promise<SecurityActionResult> {
  if (
    !currentPassword ||
    currentPassword.length > 512 ||
    (method === "code" ? !/^\d{6}$/.test(verifier) : !verifier || verifier.length > 64)
  ) {
    return { ok: false, error: "invalid" };
  }
  const response = await apiFetch("/auth/mfa/disable", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      ...(method === "code" ? { code: verifier } : { recovery_code: verifier }),
    }),
  });
  if (!response.ok) {
    return {
      ok: false,
      error: response.status === 401 ? "unauthorized" : "failed",
    };
  }
  // The backend revokes every refresh session and increments the token version
  // when MFA is reset, so this browser must discard both local sessions too.
  await clearApiSessionCookie();
  await signOut({ redirectTo: "/login" });
  return { ok: true };
}
