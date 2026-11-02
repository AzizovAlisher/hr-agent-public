"use server";

import type { PublicOffer } from "@/lib/types";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

/** @ui-only Non-enumerating public-offer page state. */
export type PublicOfferResult =
  | { ok: true; data: PublicOffer }
  | { ok: false; code: "not_found" | "expired" | "conflict" | "request_failed" };

async function publicOfferRequest(
  token: string,
  init?: RequestInit,
): Promise<PublicOfferResult> {
  try {
    const response = await fetch(
      `${API_BASE_URL}/public/offers/${encodeURIComponent(token)}${
        init?.method === "POST" ? "/respond" : ""
      }`,
      {
        ...init,
        headers: init?.body
          ? { "Content-Type": "application/json", ...init.headers }
          : init?.headers,
        cache: "no-store",
      },
    );
    if (!response.ok) {
      const code =
        response.status === 404
          ? "not_found"
          : response.status === 410
            ? "expired"
            : response.status === 409
              ? "conflict"
              : "request_failed";
      return { ok: false, code };
    }
    return { ok: true, data: (await response.json()) as PublicOffer };
  } catch {
    return { ok: false, code: "request_failed" };
  }
}

export async function getPublicOffer(token: string): Promise<PublicOfferResult> {
  return publicOfferRequest(token);
}

export async function respondToPublicOffer(
  token: string,
  response: "accept" | "decline",
  signatureName: string | null,
): Promise<PublicOfferResult> {
  return publicOfferRequest(token, {
    method: "POST",
    body: JSON.stringify({
      response,
      signature_name: signatureName?.trim() || null,
    }),
  });
}
