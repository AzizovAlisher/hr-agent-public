import type { ApiResponseSchema } from "@/lib/generated/contracts";

export type EffectivePermissions = ApiResponseSchema<"EffectivePermissionsOut">;
export type PermissionMap = Readonly<EffectivePermissions["permissions"]>;

/** @ui-only Permission-derived control state for candidate workspaces. */
export type CandidateWorkspaceAccess = {
  readCandidates: boolean;
  writeCandidates: boolean;
  exportCandidates: boolean;
  viewCandidateContacts: boolean;
  sendCandidateMessages: boolean;
  createPositions: boolean;
  changeApplicationStage: boolean;
  useSensitiveAi: boolean;
};

/** @ui-only Permission-derived control state for settings navigation. */
export type SettingsAccess = {
  manageProfile: boolean;
  readCalendar: boolean;
  viewCalendar: boolean;
  manageCalendar: boolean;
  readTeam: boolean;
  viewTeam: boolean;
  manageTeam: boolean;
  readBranding: boolean;
  viewBranding: boolean;
  manageBranding: boolean;
  viewCareers: boolean;
  readCommunicationAccounts: boolean;
  viewCommunicationAccounts: boolean;
  manageCommunicationAccounts: boolean;
  rotateCommunicationCredentials: boolean;
  readCommunicationTemplates: boolean;
  viewCommunicationTemplates: boolean;
  manageCommunicationTemplates: boolean;
  readScorecards: boolean;
  viewScorecards: boolean;
  manageScorecards: boolean;
  readWorkflows: boolean;
  viewWorkflows: boolean;
  manageWorkflows: boolean;
  executeWorkflows: boolean;
  readOffers: boolean;
  viewOffers: boolean;
  manageOffers: boolean;
  viewPositions: boolean;
};

export const NO_PERMISSIONS: PermissionMap = Object.freeze({});

const API_ROLES = new Set<EffectivePermissions["role"]>([
  "admin",
  "recruiter",
  "hiring_manager",
  "interviewer",
  "restricted",
]);

export function normalizeEffectivePermissions(value: unknown): EffectivePermissions | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as { role?: unknown; permissions?: unknown };
  if (
    typeof candidate.role !== "string" ||
    !API_ROLES.has(candidate.role as EffectivePermissions["role"]) ||
    !candidate.permissions ||
    typeof candidate.permissions !== "object" ||
    Array.isArray(candidate.permissions)
  ) {
    return null;
  }

  const permissions: Record<string, boolean> = {};
  for (const [permission, allowed] of Object.entries(candidate.permissions)) {
    if (typeof allowed === "boolean") permissions[permission] = allowed;
  }
  return {
    role: candidate.role as EffectivePermissions["role"],
    permissions,
  };
}

export function hasPermission(
  permissions: PermissionMap,
  permission: string,
): boolean {
  return permissions[permission] === true;
}

export function hasEveryPermission(
  permissions: PermissionMap,
  required: readonly string[],
): boolean {
  return required.every((permission) => hasPermission(permissions, permission));
}

export function hasAnyPermission(
  permissions: PermissionMap,
  required: readonly string[],
): boolean {
  return required.some((permission) => hasPermission(permissions, permission));
}

/**
 * One UI contract for the candidate/position workspace. The API remains the
 * authority; this prevents read-only roles and explicit deny overrides from
 * seeing controls that can only end in a forbidden mutation.
 */
export function getCandidateWorkspaceAccess(
  permissions: PermissionMap,
): CandidateWorkspaceAccess {
  const readCandidates = hasPermission(permissions, "candidates.read");
  const writeCandidates = hasPermission(permissions, "candidates.write");
  const viewCandidateContacts = hasPermission(
    permissions,
    "candidates.contact",
  );
  const useSensitiveAi = hasEveryPermission(permissions, [
    "ai.use",
    "ai.sensitive_context",
  ]);

  return {
    readCandidates,
    writeCandidates,
    exportCandidates: hasPermission(permissions, "candidates.export"),
    viewCandidateContacts,
    sendCandidateMessages: hasEveryPermission(permissions, [
      "communications.read",
      "communications.send",
      "candidates.contact",
    ]),
    createPositions: hasEveryPermission(permissions, [
      "candidates.write",
      "ai.use",
    ]),
    changeApplicationStage: hasPermission(
      permissions,
      "applications.disposition",
    ),
    useSensitiveAi,
  };
}

export function getSettingsAccess(
  permissions: PermissionMap,
): SettingsAccess {
  const readCalendar = hasPermission(permissions, "scheduling.read");
  const manageCalendar = hasPermission(permissions, "scheduling.manage");
  const readTeam = hasPermission(permissions, "enterprise.read");
  const manageTeam = hasPermission(permissions, "enterprise.manage");
  const readBranding = readTeam;
  const readCommunicationAccounts = hasPermission(
    permissions,
    "communications.read",
  );
  const manageCommunicationAccounts = hasEveryPermission(permissions, [
    "integrations.manage",
    "integrations.credentials",
  ]);
  const readCommunicationTemplates = hasPermission(
    permissions,
    "communications.read",
  );
  const manageCommunicationTemplates = hasPermission(
    permissions,
    "communications.manage",
  );
  const readScorecards = hasPermission(permissions, "interviews.read");
  const manageScorecards = hasPermission(permissions, "interviews.manage");
  const readWorkflows = hasPermission(permissions, "workflows.read");
  const manageWorkflows = hasPermission(permissions, "workflows.manage");
  const executeWorkflows = hasPermission(permissions, "workflows.execute");
  const readOffers = hasEveryPermission(permissions, [
    "offers.read",
    "offers.compensation",
  ]);
  const manageOffers = hasEveryPermission(permissions, [
    "offers.manage",
    "offers.compensation",
  ]);

  return {
    manageProfile: hasPermission(permissions, "account.manage"),
    readCalendar,
    viewCalendar: readCalendar || manageCalendar,
    manageCalendar,
    readTeam,
    viewTeam: readTeam || manageTeam,
    manageTeam,
    readBranding,
    viewBranding: readBranding || manageTeam,
    manageBranding: manageTeam,
    viewCareers: readTeam,
    readCommunicationAccounts,
    viewCommunicationAccounts:
      readCommunicationAccounts || manageCommunicationAccounts,
    manageCommunicationAccounts,
    rotateCommunicationCredentials: manageCommunicationAccounts,
    readCommunicationTemplates,
    viewCommunicationTemplates:
      readCommunicationTemplates || manageCommunicationTemplates,
    manageCommunicationTemplates,
    readScorecards,
    viewScorecards: readScorecards || manageScorecards,
    manageScorecards,
    readWorkflows,
    viewWorkflows: readWorkflows || manageWorkflows || executeWorkflows,
    manageWorkflows,
    executeWorkflows,
    readOffers,
    viewOffers: readOffers || manageOffers,
    manageOffers,
    viewPositions: hasPermission(permissions, "candidates.read"),
  };
}

export function canUseAiWorkspace(permissions: PermissionMap): boolean {
  return hasEveryPermission(permissions, ["ai.use", "ai.sensitive_context"]);
}
