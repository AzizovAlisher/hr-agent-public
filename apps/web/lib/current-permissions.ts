import "server-only";

import { cache } from "react";

import { apiGet } from "@/lib/api-client";
import {
  NO_PERMISSIONS,
  normalizeEffectivePermissions,
  type EffectivePermissions,
} from "@/lib/permissions";

function isNextRedirect(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "digest" in error &&
    typeof (error as { digest?: unknown }).digest === "string" &&
    (error as { digest: string }).digest.startsWith("NEXT_REDIRECT")
  );
}

/**
 * Returns the API's effective permission set for the current request.
 *
 * The response is validated and failures deny every canonical action. React's
 * request cache lets layouts and pages share the same policy lookup without
 * weakening the API's authoritative checks.
 */
export const getCurrentPermissions = cache(
  async (): Promise<EffectivePermissions | null> => {
    try {
      return normalizeEffectivePermissions(
        await apiGet<EffectivePermissions>("/enterprise/permissions/me"),
      );
    } catch (error) {
      if (isNextRedirect(error)) throw error;
      return null;
    }
  },
);

export async function getCurrentPermissionMap() {
  return (await getCurrentPermissions())?.permissions ?? NO_PERMISSIONS;
}
