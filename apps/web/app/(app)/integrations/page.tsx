import { redirect } from "next/navigation";

import type {
  IntegrationConnectionPage,
  IntegrationJobPage,
  IntegrationProviderDefinition,
} from "@/app/(app)/integrations/types";
import { Topbar } from "@/components/app/topbar";
import { IntegrationAdmin } from "@/components/integrations/integration-admin";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getMessages } from "@/lib/i18n";
import { hasEveryPermission, hasPermission } from "@/lib/permissions";

export const dynamic = "force-dynamic";

/** @ui-only Server-render load state with a recoverable partial-failure flag. */
type Loaded<T> = { data: T; failed: boolean };

async function optional<T>(path: string, fallback: T): Promise<Loaded<T>> {
  try {
    return { data: await apiGet<T>(path), failed: false };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return { data: fallback, failed: true };
  }
}

async function optionalCollection<T>(path: string): Promise<Loaded<T[]>> {
  try {
    return { data: await apiGetAll<T>(path), failed: false };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return { data: [], failed: true };
  }
}

export default async function IntegrationsPage() {
  const [permissions, messages] = await Promise.all([
    getCurrentPermissionMap(),
    getMessages(),
  ]);
  const read = hasPermission(permissions, "integrations.read");
  if (!read) redirect("/settings");
  const access = {
    read,
    manage: hasPermission(permissions, "integrations.manage"),
    credentials: hasEveryPermission(permissions, [
      "integrations.manage",
      "integrations.credentials",
    ]),
  };
  const [providers, connections, jobs] = await Promise.all([
    optionalCollection<IntegrationProviderDefinition>("/integrations/providers"),
    optional<IntegrationConnectionPage>("/integrations?limit=50", {
      items: [],
      next_cursor: null,
    }),
    optional<IntegrationJobPage>("/integrations/jobs?limit=50", {
      items: [],
      next_cursor: null,
    }),
  ]);
  return (
    <>
      <Topbar
        crumbs={[
          { label: messages.settings.title, href: "/settings" },
          { label: "Integrations" },
        ]}
      />
      <div className="flex-1 overflow-y-auto px-4 py-6 sm:px-7 sm:py-8">
        <IntegrationAdmin
          access={access}
          providers={providers.data}
          initialConnections={connections.data.items}
          connectionCursor={connections.data.next_cursor}
          initialJobs={jobs.data.items}
          jobCursor={jobs.data.next_cursor}
          loadFailed={providers.failed || connections.failed || jobs.failed}
        />
      </div>
    </>
  );
}
