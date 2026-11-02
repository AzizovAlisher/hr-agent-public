"use server";

/**
 * Unauthenticated scheduling requests. Keeping these requests in a dedicated
 * module prevents an authenticated recruiter token from ever being attached
 * when an employee opens a candidate-facing scheduling page.
 */

import type {
  PublicAppointment,
  PublicAppointmentDetails,
  PublicSchedulingDetails,
  SchedulingSlotPage,
} from "@/lib/types";
import { publicBrandingUrl } from "@/lib/public-branding";
import type { ApiResponseSchema } from "@/lib/generated/contracts";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

const PUBLIC_ERROR_CODES = new Set([
  "appointment_not_found",
  "appointment_not_scheduled",
  "booking_conflict",
  "idempotency_conflict",
  "invalid_timezone",
  "link_booked",
  "link_expired",
  "link_not_found",
  "link_revoked",
  "reschedule_conflict",
  "reschedule_unavailable",
  "slot_unavailable",
  "version_conflict",
]);

/** @ui-only Non-enumerating public-scheduling page state. */
export type PublicSchedulingResult<T> =
  | { ok: true; data: T }
  | { ok: false; code: string };

function withSchedulingLogo(
  details: PublicSchedulingDetails,
): PublicSchedulingDetails {
  return {
    ...details,
    organization_logo_url: publicBrandingUrl(details.organization_logo_url),
  };
}

function withAppointmentLogo(
  details: PublicAppointmentDetails,
): PublicAppointmentDetails {
  return {
    ...details,
    organization_logo_url: publicBrandingUrl(details.organization_logo_url),
  };
}

async function errorCode(response: Response): Promise<string> {
  try {
    const value = await response.json();
    const directCode =
      value && typeof value === "object" ? Reflect.get(value, "code") : undefined;
    const detail =
      value && typeof value === "object" ? Reflect.get(value, "detail") : undefined;
    const nestedCode =
      detail && typeof detail === "object" ? Reflect.get(detail, "code") : undefined;
    const candidate =
      typeof directCode === "string"
        ? directCode
        : typeof nestedCode === "string"
          ? nestedCode
          : undefined;
    return typeof candidate === "string" && PUBLIC_ERROR_CODES.has(candidate)
      ? candidate
      : "request_failed";
  } catch {
    return "request_failed";
  }
}

async function publicRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<PublicSchedulingResult<T>> {
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: init?.body
        ? { "Content-Type": "application/json", ...init.headers }
        : init?.headers,
      cache: "no-store",
    });
    if (!response.ok) return { ok: false, code: await errorCode(response) };
    return { ok: true, data: (await response.json()) as T };
  } catch {
    return { ok: false, code: "network_error" };
  }
}

export async function getPublicSchedulingDetails(
  token: string,
): Promise<PublicSchedulingResult<PublicSchedulingDetails>> {
  const result = await publicRequest<PublicSchedulingDetails>(
    `/public/scheduling/${encodeURIComponent(token)}`,
  );
  return result.ok
    ? { ok: true, data: withSchedulingLogo(result.data) }
    : result;
}

export async function getPublicSchedulingSlots(
  token: string,
  options?: { timezone?: string | null; fromDate?: string | null; days?: number },
): Promise<PublicSchedulingResult<SchedulingSlotPage>> {
  const query = new URLSearchParams();
  if (options?.timezone) query.set("timezone", options.timezone);
  if (options?.fromDate) query.set("from_date", options.fromDate);
  query.set("days", String(options?.days ?? 14));
  return publicRequest<SchedulingSlotPage>(
    `/public/scheduling/${encodeURIComponent(token)}/slots?${query.toString()}`,
  );
}

export async function bookPublicSchedulingSlot(
  token: string,
  body: { starts_at: string; timezone: string; idempotency_key: string },
): Promise<
  PublicSchedulingResult<{
    appointment: PublicAppointment;
    managePath: string;
  }>
> {
  const result = await publicRequest<ApiResponseSchema<"PublicBookingOut">>(`/public/scheduling/${encodeURIComponent(token)}/book`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!result.ok) return result;
  const match = result.data.manage_url.match(
    /^\/public\/scheduling\/manage\/([A-Za-z0-9_-]+)$/,
  );
  if (!match) return { ok: false, code: "request_failed" };
  return {
    ok: true,
    data: {
      appointment: result.data.appointment,
      managePath: `/schedule/manage/${encodeURIComponent(match[1])}`,
    },
  };
}

export async function getPublicAppointmentDetails(
  manageToken: string,
): Promise<PublicSchedulingResult<PublicAppointmentDetails>> {
  const result = await publicRequest<PublicAppointmentDetails>(
    `/public/scheduling/manage/${encodeURIComponent(manageToken)}`,
  );
  return result.ok
    ? { ok: true, data: withAppointmentLogo(result.data) }
    : result;
}

export async function getPublicRescheduleSlots(
  manageToken: string,
  options?: { timezone?: string | null; fromDate?: string | null; days?: number },
): Promise<PublicSchedulingResult<SchedulingSlotPage>> {
  const query = new URLSearchParams();
  if (options?.timezone) query.set("timezone", options.timezone);
  if (options?.fromDate) query.set("from_date", options.fromDate);
  query.set("days", String(options?.days ?? 14));
  return publicRequest<SchedulingSlotPage>(
    `/public/scheduling/manage/${encodeURIComponent(manageToken)}/slots?${query.toString()}`,
  );
}

export async function reschedulePublicAppointment(
  manageToken: string,
  body: { starts_at: string; timezone: string; expected_version: number },
): Promise<PublicSchedulingResult<PublicAppointmentDetails>> {
  const result = await publicRequest<PublicAppointmentDetails>(
    `/public/scheduling/manage/${encodeURIComponent(manageToken)}/reschedule`,
    { method: "POST", body: JSON.stringify(body) },
  );
  return result.ok
    ? { ok: true, data: withAppointmentLogo(result.data) }
    : result;
}

export async function cancelPublicAppointment(
  manageToken: string,
  expectedVersion: number,
): Promise<PublicSchedulingResult<PublicAppointmentDetails>> {
  const result = await publicRequest<PublicAppointmentDetails>(
    `/public/scheduling/manage/${encodeURIComponent(manageToken)}/cancel`,
    {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
  );
  return result.ok
    ? { ok: true, data: withAppointmentLogo(result.data) }
    : result;
}
