import "server-only";

import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes,
} from "node:crypto";
import { cookies } from "next/headers";

import type { components } from "@/lib/generated/api";

type Organization = components["schemas"]["app__schemas__auth__OrganizationOut"];
type LoginResponse = components["schemas"]["LoginResponse"];

export const API_SESSION_COOKIE = "hr_api_session";
export const MFA_CHALLENGE_COOKIE = "hr_mfa_challenge";
export const INTERNAL_ACCESS_HEADER = "x-hr-api-access-token";

const API_SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 90;
const MFA_CHALLENGE_MAX_AGE_SECONDS = 60 * 15;
const IDENTITY_TICKET_MAX_AGE_MS = 60_000;

export type AuthIdentity = {
  id: string;
  email: string;
  role: string;
  locale: string;
  organization: Organization;
};

export type ApiSession = AuthIdentity & {
  accessToken: string;
  refreshToken: string;
  accessExpiresAt: number;
};

/** @local-only Encrypted MFA challenge-cookie payload. */
export type MfaChallenge = {
  token: string;
  email: string;
  purpose: "setup" | "login";
  expiresAt: number;
};

type IdentityTicket = AuthIdentity & { expiresAt: number };

function isStrongProductionSecret(value: string | undefined): value is string {
  return Boolean(
    value &&
      value.length >= 43 &&
      new Set(value).size >= 16 &&
      !/change[-_ ]?me|replace[-_ ]?me|example/i.test(value),
  );
}

function sessionRootSecret(): string {
  const explicit = process.env.AUTH_SESSION_SECRET;
  if (process.env.NODE_ENV === "production") {
    const authJsSecret = process.env.AUTH_SECRET ?? process.env.NEXTAUTH_SECRET;
    if (!isStrongProductionSecret(authJsSecret)) {
      throw new Error(
        "Production requires a high-entropy AUTH_SECRET or NEXTAUTH_SECRET (at least 32 random bytes)",
      );
    }
    if (
      !isStrongProductionSecret(explicit) ||
      explicit === authJsSecret
    ) {
      throw new Error(
        "Production requires an independent, high-entropy AUTH_SESSION_SECRET (at least 32 random bytes)",
      );
    }
    return explicit;
  }
  const developmentSecret =
    explicit ?? process.env.AUTH_SECRET ?? process.env.NEXTAUTH_SECRET;
  if (!developmentSecret || developmentSecret.length < 24) {
    throw new Error(
      "AUTH_SESSION_SECRET or the development Auth.js secret must contain at least 24 characters",
    );
  }
  return developmentSecret;
}

export function validateAuthSessionConfiguration(): void {
  void sessionRootSecret();
}

function encryptionKey(purpose: string): Buffer {
  const secret = sessionRootSecret();
  return createHash("sha256").update(`hr-web/${purpose}/v1:${secret}`).digest();
}

if (process.env.NODE_ENV === "production") validateAuthSessionConfiguration();

function seal(value: unknown, purpose: string): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", encryptionKey(purpose), iv);
  const ciphertext = Buffer.concat([
    cipher.update(JSON.stringify(value), "utf8"),
    cipher.final(),
  ]);
  return Buffer.concat([iv, cipher.getAuthTag(), ciphertext]).toString("base64url");
}

function open(value: string | undefined, purpose: string, maxLength: number): unknown {
  if (!value || value.length > maxLength) return null;
  try {
    const encoded = Buffer.from(value, "base64url");
    if (encoded.length < 29) return null;
    const decipher = createDecipheriv(
      "aes-256-gcm",
      encryptionKey(purpose),
      encoded.subarray(0, 12),
    );
    decipher.setAuthTag(encoded.subarray(12, 28));
    return JSON.parse(
      Buffer.concat([
        decipher.update(encoded.subarray(28)),
        decipher.final(),
      ]).toString("utf8"),
    ) as unknown;
  } catch {
    return null;
  }
}

function validOrganization(value: unknown): value is Organization {
  if (!value || typeof value !== "object") return false;
  const org = value as Record<string, unknown>;
  return (
    typeof org.id === "string" &&
    typeof org.name === "string" &&
    typeof org.slug === "string"
  );
}

function validIdentity(value: unknown): value is AuthIdentity {
  if (!value || typeof value !== "object") return false;
  const identity = value as Record<string, unknown>;
  return (
    typeof identity.id === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      identity.id,
    ) &&
    typeof identity.email === "string" &&
    identity.email.length <= 320 &&
    typeof identity.role === "string" &&
    identity.role.length <= 64 &&
    typeof identity.locale === "string" &&
    identity.locale.length <= 16 &&
    validOrganization(identity.organization)
  );
}

export function apiSessionFromLogin(data: LoginResponse, email: string): ApiSession | null {
  if (
    typeof data.access_token !== "string" ||
    data.access_token.length < 20 ||
    data.access_token.length > 8_192 ||
    typeof data.refresh_token !== "string" ||
    data.refresh_token.length < 20 ||
    data.refresh_token.length > 512 ||
    typeof data.expires_in !== "number" ||
    !Number.isFinite(data.expires_in) ||
    data.expires_in < 30 ||
    data.expires_in > 86_400
  ) {
    return null;
  }
  const identity: AuthIdentity = {
    id: data.user_id,
    email: email.trim().toLowerCase(),
    role: data.role,
    locale: data.locale,
    organization: data.organization,
  };
  if (!validIdentity(identity)) return null;
  return {
    ...identity,
    accessToken: data.access_token,
    refreshToken: data.refresh_token,
    accessExpiresAt: Date.now() + data.expires_in * 1_000,
  };
}

export function sealApiSession(session: ApiSession): string {
  return seal(session, "api-session");
}

export function openApiSession(value: string | undefined): ApiSession | null {
  const parsed = open(value, "api-session", 12_000);
  if (!validIdentity(parsed)) return null;
  const session = parsed as Partial<ApiSession>;
  if (
    typeof session.accessToken !== "string" ||
    session.accessToken.length < 20 ||
    session.accessToken.length > 8_192 ||
    typeof session.refreshToken !== "string" ||
    session.refreshToken.length < 20 ||
    session.refreshToken.length > 512 ||
    typeof session.accessExpiresAt !== "number" ||
    !Number.isFinite(session.accessExpiresAt)
  ) {
    return null;
  }
  return session as ApiSession;
}

export function sealIdentityTicket(identity: AuthIdentity): string {
  return seal(
    { ...identity, expiresAt: Date.now() + IDENTITY_TICKET_MAX_AGE_MS },
    "identity-ticket",
  );
}

export function openIdentityTicket(value: string | undefined): AuthIdentity | null {
  const parsed = open(value, "identity-ticket", 8_000);
  if (!validIdentity(parsed)) return null;
  const ticket = parsed as Partial<IdentityTicket>;
  if (
    typeof ticket.expiresAt !== "number" ||
    ticket.expiresAt <= Date.now() ||
    ticket.expiresAt > Date.now() + IDENTITY_TICKET_MAX_AGE_MS + 5_000
  ) {
    return null;
  }
  const { expiresAt: _expiresAt, ...identity } = ticket as IdentityTicket;
  return identity;
}

export function sealMfaChallenge(challenge: MfaChallenge): string {
  return seal(challenge, "mfa-challenge");
}

export function openMfaChallenge(value: string | undefined): MfaChallenge | null {
  const parsed = open(value, "mfa-challenge", 10_000);
  if (!parsed || typeof parsed !== "object") return null;
  const challenge = parsed as Partial<MfaChallenge>;
  if (
    typeof challenge.token !== "string" ||
    challenge.token.length < 20 ||
    challenge.token.length > 4_096 ||
    typeof challenge.email !== "string" ||
    challenge.email.length > 320 ||
    (challenge.purpose !== "setup" && challenge.purpose !== "login") ||
    typeof challenge.expiresAt !== "number" ||
    challenge.expiresAt <= Date.now()
  ) {
    return null;
  }
  return challenge as MfaChallenge;
}

function secureCookie(): boolean {
  return process.env.NODE_ENV === "production";
}

export function apiSessionCookieOptions() {
  return {
    httpOnly: true,
    secure: secureCookie(),
    sameSite: "lax" as const,
    path: "/",
    maxAge: API_SESSION_MAX_AGE_SECONDS,
  };
}

export function mfaChallengeCookieOptions() {
  return {
    httpOnly: true,
    secure: secureCookie(),
    sameSite: "strict" as const,
    path: "/login",
    maxAge: MFA_CHALLENGE_MAX_AGE_SECONDS,
  };
}

export async function currentApiSession(): Promise<ApiSession | null> {
  const store = await cookies();
  return openApiSession(store.get(API_SESSION_COOKIE)?.value);
}

export async function currentMfaChallenge(): Promise<MfaChallenge | null> {
  const store = await cookies();
  return openMfaChallenge(store.get(MFA_CHALLENGE_COOKIE)?.value);
}

export async function setApiSessionCookie(session: ApiSession): Promise<void> {
  const store = await cookies();
  store.set(API_SESSION_COOKIE, sealApiSession(session), apiSessionCookieOptions());
}

export async function clearApiSessionCookie(): Promise<void> {
  const store = await cookies();
  store.set(API_SESSION_COOKIE, "", {
    ...apiSessionCookieOptions(),
    maxAge: 0,
    expires: new Date(0),
  });
}

export async function setMfaChallengeCookie(challenge: MfaChallenge): Promise<void> {
  const store = await cookies();
  store.set(
    MFA_CHALLENGE_COOKIE,
    sealMfaChallenge(challenge),
    mfaChallengeCookieOptions(),
  );
}

export async function clearMfaChallengeCookie(): Promise<void> {
  const store = await cookies();
  store.set(MFA_CHALLENGE_COOKIE, "", {
    ...mfaChallengeCookieOptions(),
    maxAge: 0,
    expires: new Date(0),
  });
}
