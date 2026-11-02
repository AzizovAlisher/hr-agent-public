"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { z } from "zod";

import { signIn } from "@/auth";
import { unauthenticatedAuthFetch } from "@/lib/auth-api";
import type { AuthFlowError, AuthFlowState } from "@/lib/auth-flow-types";
import type { components } from "@/lib/generated/api";
import {
  apiSessionFromLogin,
  clearApiSessionCookie,
  clearMfaChallengeCookie,
  currentMfaChallenge,
  sealIdentityTicket,
  setApiSessionCookie,
  setMfaChallengeCookie,
} from "@/lib/auth-session";

type LoginResponse = components["schemas"]["LoginResponse"];
type MfaSetupResponse = components["schemas"]["MfaSetupResponse"];
type MfaConfirmResponse = components["schemas"]["MfaConfirmResponse"];

const loginSchema = z.object({
  email: z.email().max(320),
  password: z.string().min(1).max(512),
});

function errorState(
  step: AuthFlowState["step"],
  error: AuthFlowError,
  retryAfter?: number,
): AuthFlowState {
  return { step, error, retryAfter };
}

function responseError(
  response: Response,
  step: AuthFlowState["step"],
  invalidError: AuthFlowError,
): AuthFlowState {
  if (response.status === 429) {
    const raw = Number(response.headers.get("retry-after"));
    return errorState(step, "rate_limited", Number.isFinite(raw) ? raw : undefined);
  }
  if (response.status >= 500) return errorState(step, "service_unavailable");
  return errorState(step, invalidError);
}

async function establishSession(
  data: LoginResponse,
  email: string,
): Promise<boolean> {
  const session = apiSessionFromLogin(data, email);
  if (!session) return false;
  await setApiSessionCookie(session);
  await clearMfaChallengeCookie();
  const store = await cookies();
  store.set("locale", session.locale === "en" ? "en" : "ru", {
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
    sameSite: "lax",
  });
  try {
    const destination = await signIn("credentials", {
      sessionTicket: sealIdentityTicket(session),
      redirect: false,
      redirectTo: "/my-work",
    });
    if (typeof destination !== "string" || destination.includes("error=")) {
      await clearApiSessionCookie();
      return false;
    }
    return true;
  } catch {
    await clearApiSessionCookie();
    return false;
  }
}

async function login(formData: FormData): Promise<AuthFlowState> {
  const parsed = loginSchema.safeParse({
    email: formData.get("email"),
    password: formData.get("password"),
  });
  if (!parsed.success) return errorState("credentials", "invalid_request");
  const email = parsed.data.email.trim().toLowerCase();
  let response: Response;
  try {
    response = await unauthenticatedAuthFetch("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password: parsed.data.password }),
    });
  } catch {
    return errorState("credentials", "service_unavailable");
  }
  if (!response.ok) return responseError(response, "credentials", "invalid_credentials");
  const data = (await response.json()) as LoginResponse;

  const purpose = data.mfa_setup_required
    ? "setup"
    : data.mfa_required
      ? "login"
      : null;
  if (purpose) {
    if (typeof data.challenge_token !== "string" || data.challenge_token.length < 20) {
      return errorState("credentials", "service_unavailable");
    }
    await clearApiSessionCookie();
    await setMfaChallengeCookie({
      token: data.challenge_token,
      email,
      purpose,
      expiresAt: Date.now() + 15 * 60_000,
    });
    return { step: purpose === "setup" ? "setup" : "verify" };
  }

  if (!(await establishSession(data, email))) {
    return errorState("credentials", "service_unavailable");
  }
  redirect("/my-work");
}

async function setupMfa(): Promise<AuthFlowState> {
  const challenge = await currentMfaChallenge();
  if (!challenge || challenge.purpose !== "setup") {
    return errorState("credentials", "challenge_expired");
  }
  let response: Response;
  try {
    response = await unauthenticatedAuthFetch("/auth/mfa/setup", {
      method: "POST",
      body: JSON.stringify({ challenge_token: challenge.token }),
    });
  } catch {
    return errorState("setup", "service_unavailable");
  }
  if (response.status === 409) return errorState("setup", "setup_already_started");
  if (!response.ok) return responseError(response, "setup", "challenge_expired");
  const data = (await response.json()) as MfaSetupResponse;
  if (
    typeof data.secret !== "string" ||
    data.secret.length < 16 ||
    typeof data.provisioning_uri !== "string" ||
    !data.provisioning_uri.startsWith("otpauth://")
  ) {
    return errorState("setup", "service_unavailable");
  }
  return {
    step: "setup",
    setup: { secret: data.secret, provisioningUri: data.provisioning_uri },
  };
}

async function confirmMfa(
  formData: FormData,
  previous: AuthFlowState,
): Promise<AuthFlowState> {
  const setupError = (
    error: AuthFlowError,
    retryAfter?: number,
  ): AuthFlowState => ({
    step: "setup",
    error,
    retryAfter,
    setup: previous.setup,
  });
  const challenge = await currentMfaChallenge();
  if (!challenge || challenge.purpose !== "setup") {
    return errorState("credentials", "challenge_expired");
  }
  const code = formData.get("code");
  if (typeof code !== "string" || !/^\d{6}$/.test(code)) {
    return setupError("invalid_request");
  }
  let response: Response;
  try {
    response = await unauthenticatedAuthFetch("/auth/mfa/confirm", {
      method: "POST",
      body: JSON.stringify({ challenge_token: challenge.token, code }),
    });
  } catch {
    return setupError("service_unavailable");
  }
  if (!response.ok) {
    const responseState = responseError(response, "setup", "invalid_code");
    return setupError(responseState.error ?? "invalid_code", responseState.retryAfter);
  }
  const data = (await response.json()) as MfaConfirmResponse;
  if (
    !Array.isArray(data.recovery_codes) ||
    data.recovery_codes.length < 1 ||
    data.recovery_codes.some((item) => typeof item !== "string" || !item)
  ) {
    return setupError("service_unavailable");
  }
  if (!(await establishSession(data, challenge.email))) {
    return setupError("service_unavailable");
  }
  return { step: "recovery", recoveryCodes: data.recovery_codes };
}

async function verifyMfa(formData: FormData): Promise<AuthFlowState> {
  const challenge = await currentMfaChallenge();
  if (!challenge || challenge.purpose !== "login") {
    return errorState("credentials", "challenge_expired");
  }
  const method = formData.get("method") === "recovery" ? "recovery" : "code";
  const verifier = formData.get("verifier");
  if (
    typeof verifier !== "string" ||
    (method === "code" ? !/^\d{6}$/.test(verifier) : verifier.length < 1 || verifier.length > 64)
  ) {
    return errorState("verify", "invalid_request");
  }
  const body =
    method === "code"
      ? { challenge_token: challenge.token, code: verifier }
      : { challenge_token: challenge.token, recovery_code: verifier.trim() };
  let response: Response;
  try {
    response = await unauthenticatedAuthFetch("/auth/mfa/verify", {
      method: "POST",
      body: JSON.stringify(body),
    });
  } catch {
    return errorState("verify", "service_unavailable");
  }
  if (!response.ok) return responseError(response, "verify", "invalid_code");
  const data = (await response.json()) as LoginResponse;
  if (!(await establishSession(data, challenge.email))) {
    return errorState("verify", "service_unavailable");
  }
  redirect("/my-work");
}

export async function authFlowAction(
  _previous: AuthFlowState,
  formData: FormData,
): Promise<AuthFlowState> {
  const intent = formData.get("intent");
  if (intent === "login") return login(formData);
  if (intent === "setup") return setupMfa();
  if (intent === "confirm") return confirmMfa(formData, _previous);
  if (intent === "verify") return verifyMfa(formData);
  if (intent === "restart") {
    await clearMfaChallengeCookie();
    await clearApiSessionCookie();
    return { step: "credentials" };
  }
  return errorState("credentials", "invalid_request");
}
