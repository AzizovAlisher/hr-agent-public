import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { EnterpriseControls } from "@/components/enterprise/enterprise-controls";
import type {
  EnterpriseControlAccess,
  EnterpriseControlsProps,
} from "@/components/enterprise/enterprise-controls";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getMessages } from "@/lib/i18n";
import {
  hasEveryPermission,
  hasPermission,
} from "@/lib/permissions";

export const dynamic = "force-dynamic";

async function optional<T>(path: string, fallback: T): Promise<T> {
  try {
    return await apiGet<T>(path);
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return fallback;
  }
}

async function optionalCollection<T>(path: string): Promise<T[]> {
  try {
    return await apiGetAll<T>(path);
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return [];
  }
}

export default async function EnterprisePage() {
  const [session, messages, permissions] = await Promise.all([
    auth(),
    getMessages(),
    getCurrentPermissionMap(),
  ]);
  if (!session) redirect("/login");

  const enterpriseRead = hasPermission(permissions, "enterprise.read");
  const enterpriseManage = hasPermission(permissions, "enterprise.manage");
  const integrationsRead = hasPermission(permissions, "integrations.read");
  const access: EnterpriseControlAccess = {
    viewPolicy: enterpriseRead,
    managePolicy: enterpriseRead && enterpriseManage,
    managePermissions: enterpriseManage,
    viewAudit: hasPermission(permissions, "enterprise.audit_export"),
    manageRecovery: enterpriseManage,
    downloadBackup: hasPermission(permissions, "enterprise.backup_export"),
    viewApiKeys: integrationsRead,
    manageApiKeys: hasEveryPermission(permissions, [
      "integrations.manage",
      "integrations.credentials",
    ]),
    viewSso: integrationsRead,
    manageSso: hasEveryPermission(permissions, [
      "integrations.manage",
      "integrations.credentials",
    ]),
    viewModel: integrationsRead,
    manageModel: hasEveryPermission(permissions, [
      "ai.manage",
      "ai.managed_provider",
    ]),
    viewEntitlements: enterpriseRead,
    viewUsage: enterpriseManage,
  };

  const [
    policy,
    overrides,
    audit,
    trash,
    retention,
    apiKeys,
    sso,
    modelConfig,
    entitlements,
    usage,
  ] =
    await Promise.all([
      access.viewPolicy
        ? optional<EnterpriseControlsProps["initialPolicy"]>("/enterprise/policy", null)
        : Promise.resolve(null),
      access.managePermissions
        ? optionalCollection<EnterpriseControlsProps["initialOverrides"][number]>(
            "/enterprise/permissions",
          )
        : Promise.resolve([]),
      access.viewAudit
        ? optional<EnterpriseControlsProps["initialAudit"]>("/enterprise/audit?limit=50", {
            items: [],
            next_cursor: null,
          })
        : Promise.resolve({ items: [], next_cursor: null }),
      access.manageRecovery
        ? optional<EnterpriseControlsProps["initialTrash"]>("/enterprise/trash?limit=200", {
            items: [],
            next_cursor: null,
          })
        : Promise.resolve({ items: [], next_cursor: null }),
      access.manageRecovery
        ? optional<EnterpriseControlsProps["initialRetention"]>(
            "/enterprise/retention/preview",
            null,
          )
        : Promise.resolve(null),
      access.viewApiKeys
        ? optionalCollection<EnterpriseControlsProps["initialApiKeys"][number]>(
            "/enterprise/api-keys",
          )
        : Promise.resolve([]),
      access.viewSso
        ? optional<EnterpriseControlsProps["initialSso"]>("/enterprise/sso", null)
        : Promise.resolve(null),
      access.viewModel
        ? optional<EnterpriseControlsProps["initialModelConfig"]>(
            "/enterprise/model-configuration",
            null,
          )
        : Promise.resolve(null),
      access.viewEntitlements
        ? optional<EnterpriseControlsProps["initialEntitlements"]>(
            "/enterprise/entitlements",
            null,
          )
        : Promise.resolve(null),
      access.viewUsage
        ? optional<EnterpriseControlsProps["initialUsage"]>(
            "/enterprise/usage?limit=50",
            null,
          )
        : Promise.resolve(null),
    ]);

  return (
    <>
      <Topbar
        crumbs={[
          { label: messages.settings.title, href: "/settings" },
          { label: session.user?.locale === "en" ? "Enterprise controls" : "Контроль и безопасность" },
        ]}
        actions={<LocaleSwitcher />}
      />
      <div className="flex-1 overflow-y-auto px-7 py-8">
        <EnterpriseControls
          access={access}
          initialPolicy={policy}
          initialOverrides={overrides}
          initialAudit={audit}
          initialTrash={trash}
          initialRetention={retention}
          initialApiKeys={apiKeys}
          initialSso={sso}
          initialModelConfig={modelConfig}
          initialEntitlements={entitlements}
          initialUsage={usage}
        />
      </div>
    </>
  );
}
