import "server-only";

import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";

export const ASSESSMENT_SESSION_COOKIE = "hr_assessment_session";

/** @local-only Encrypted cookie payload; never crosses the API boundary. */
type SealedAssessmentSession = {
  token: string;
  expiresAt: number;
};

function encryptionKey(): Buffer {
  const secret =
    process.env.ASSESSMENT_SESSION_SECRET ??
    process.env.AUTH_SECRET ??
    process.env.NEXTAUTH_SECRET;
  if (!secret || secret.length < 24) {
    throw new Error(
      "ASSESSMENT_SESSION_SECRET or NEXTAUTH_SECRET must contain at least 24 characters",
    );
  }
  return createHash("sha256").update(`assessment-session:${secret}`).digest();
}

export function sealAssessmentSession(token: string, expiresAt: Date): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", encryptionKey(), iv);
  const payload: SealedAssessmentSession = {
    token,
    expiresAt: expiresAt.getTime(),
  };
  const ciphertext = Buffer.concat([
    cipher.update(JSON.stringify(payload), "utf8"),
    cipher.final(),
  ]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, ciphertext]).toString("base64url");
}

export function openAssessmentSession(value: string | undefined): SealedAssessmentSession | null {
  if (!value || value.length > 2_048) return null;
  try {
    const encoded = Buffer.from(value, "base64url");
    if (encoded.length < 29) return null;
    const iv = encoded.subarray(0, 12);
    const tag = encoded.subarray(12, 28);
    const ciphertext = encoded.subarray(28);
    const decipher = createDecipheriv("aes-256-gcm", encryptionKey(), iv);
    decipher.setAuthTag(tag);
    const parsed = JSON.parse(
      Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString("utf8"),
    ) as Partial<SealedAssessmentSession>;
    if (
      typeof parsed.token !== "string" ||
      !/^asm_[A-Za-z0-9_-]{20,220}$/.test(parsed.token) ||
      typeof parsed.expiresAt !== "number" ||
      parsed.expiresAt <= Date.now()
    ) {
      return null;
    }
    return { token: parsed.token, expiresAt: parsed.expiresAt };
  } catch {
    return null;
  }
}
