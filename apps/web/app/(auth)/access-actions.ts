"use server";

import { z } from "zod";

import { unauthenticatedAuthFetch } from "@/lib/auth-api";

/** @ui-only Password form state; never sent as an API payload. */
export type PasswordAccessState = {
  status: "idle" | "success" | "error";
  error?: "form_invalid" | "invalid" | "gone" | "unavailable";
};

/** @ui-only Reset-request form state; never sent as an API payload. */
export type ResetRequestState = {
  status: "idle" | "accepted" | "error";
};

const tokenSchema = z.string().regex(/^[A-Za-z0-9_-]{32,128}$/);
const emailSchema = z.email().max(320);
const passwordSchema = z
  .string()
  .min(8)
  .max(512)
  .refine((value) => new TextEncoder().encode(value).length <= 72);

function accessError(response: Response): PasswordAccessState {
  if (response.status === 404) return { status: "error", error: "invalid" };
  if (response.status === 410) return { status: "error", error: "gone" };
  return { status: "error", error: "unavailable" };
}

async function submitPassword(
  purpose: "invitation" | "password_reset",
  token: string,
  formData: FormData,
): Promise<PasswordAccessState> {
  if (!tokenSchema.safeParse(token).success) {
    return { status: "error", error: "invalid" };
  }
  const password = formData.get("password");
  const confirmation = formData.get("password_confirmation");
  if (
    typeof password !== "string" ||
    password !== confirmation ||
    !passwordSchema.safeParse(password).success
  ) {
    return { status: "error", error: "form_invalid" };
  }

  const path =
    purpose === "invitation"
      ? `/auth/invitations/${encodeURIComponent(token)}/accept`
      : `/auth/password-reset/${encodeURIComponent(token)}`;
  try {
    const response = await unauthenticatedAuthFetch(path, {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    return response.ok ? { status: "success" } : accessError(response);
  } catch {
    return { status: "error", error: "unavailable" };
  }
}

export async function acceptInvitationAction(
  token: string,
  _previous: PasswordAccessState,
  formData: FormData,
): Promise<PasswordAccessState> {
  return submitPassword("invitation", token, formData);
}

export async function confirmPasswordResetAction(
  token: string,
  _previous: PasswordAccessState,
  formData: FormData,
): Promise<PasswordAccessState> {
  return submitPassword("password_reset", token, formData);
}

export async function requestPasswordResetAction(
  _previous: ResetRequestState,
  formData: FormData,
): Promise<ResetRequestState> {
  const email = formData.get("email");
  if (typeof email !== "string" || !emailSchema.safeParse(email).success) {
    return { status: "error" };
  }
  try {
    const response = await unauthenticatedAuthFetch("/auth/password-reset/request", {
      method: "POST",
      body: JSON.stringify({ email: email.trim().toLowerCase() }),
    });
    return response.ok ? { status: "accepted" } : { status: "error" };
  } catch {
    return { status: "error" };
  }
}
