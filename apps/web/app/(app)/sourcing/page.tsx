import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { SourcingWorkspace } from "@/components/sourcing/sourcing-workspace";
import type { SourcingAccess } from "@/components/sourcing/sourcing-workspace";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getMessages } from "@/lib/i18n";
import {
  hasEveryPermission,
  hasPermission,
} from "@/lib/permissions";
import type {
  IntegrationConnectionPage,
} from "@/app/(app)/integrations/types";
import type {
  ReferralLink,
  SourceAnalytics,
  SourceImportRunPage,
  SourceIntegrationPage,
  SourcingLoadFailures,
  SourcingPosition,
} from "@/app/(app)/sourcing/types";

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

export default async function SourcingPage() {
  const [session, messages, permissions] = await Promise.all([
    auth(),
    getMessages(),
    getCurrentPermissionMap(),
  ]);
  if (!session) redirect("/login");
  const locale = session.user?.locale === "en" ? "en" : "ru";

  const integrationsRead = hasPermission(permissions, "integrations.read");
  const candidatesRead = hasPermission(permissions, "candidates.read");
  const access: SourcingAccess = {
    viewConnections: integrationsRead,
    manageConnections: hasPermission(permissions, "integrations.manage"),
    rotateCredentials: hasEveryPermission(permissions, [
      "integrations.manage",
      "integrations.credentials",
    ]),
    viewRuns: integrationsRead,
    importCandidates: hasEveryPermission(permissions, [
      "integrations.read",
      "sourcing.manage",
    ]),
    viewReferrals:
      candidatesRead && hasPermission(permissions, "sourcing.read"),
    manageReferrals:
      candidatesRead && hasPermission(permissions, "sourcing.manage"),
    publish: hasEveryPermission(permissions, [
      "candidates.read",
      "integrations.read",
      "sourcing.publish",
    ]),
    viewAnalytics: hasPermission(permissions, "analytics.read"),
  };

  const needsIntegrations = integrationsRead;
  const needsPositions =
    access.viewReferrals || access.manageReferrals || access.publish;

  const [integrations, providerConnections, runs, positions, analytics] = await Promise.all([
      needsIntegrations
        ? optional<SourceIntegrationPage>("/source-integrations?limit=50", {
            items: [],
            next_cursor: null,
          })
        : Promise.resolve({
            data: { items: [], next_cursor: null },
            failed: false,
          } as Loaded<SourceIntegrationPage>),
      needsIntegrations
        ? optional<IntegrationConnectionPage>("/integrations?limit=100", {
            items: [],
            next_cursor: null,
          })
        : Promise.resolve({
            data: { items: [], next_cursor: null },
            failed: false,
          } as Loaded<IntegrationConnectionPage>),
      access.viewRuns
        ? optional<SourceImportRunPage>("/source-integrations/runs?limit=50", {
            items: [],
            next_cursor: null,
          })
        : Promise.resolve({
            data: { items: [], next_cursor: null },
            failed: false,
          } as Loaded<SourceImportRunPage>),
      needsPositions
        ? optionalCollection<SourcingPosition>("/positions")
        : Promise.resolve({ data: [], failed: false } as Loaded<SourcingPosition[]>),
      access.viewAnalytics
        ? optional<SourceAnalytics>("/sourcing/analytics", {
            items: [],
            total_applications: 0,
          })
        : Promise.resolve({
            data: { items: [], total_applications: 0 },
            failed: false,
          } as Loaded<SourceAnalytics>),
    ]);

  const initialPositionId = positions.data[0]?.id ?? "";
  const referrals =
    access.viewReferrals && initialPositionId
      ? await optionalCollection<ReferralLink>(
          `/positions/${encodeURIComponent(initialPositionId)}/referral-links`,
        )
      : ({ data: [], failed: false } as Loaded<ReferralLink[]>);
  const failures: SourcingLoadFailures = {
    integrations: integrations.failed,
    providerConnections: providerConnections.failed,
    runs: runs.failed,
    positions: positions.failed,
    analytics: analytics.failed,
    referrals: referrals.failed,
  };
  const publicApiBaseUrl = (
    process.env.NEXT_PUBLIC_API_BASE_URL ??
    process.env.API_BASE_URL ??
    "http://localhost:8000"
  ).replace(/\/$/, "");

  return (
    <>
      <Topbar
        crumbs={[
          { label: messages.settings.title, href: "/settings" },
          { label: locale === "en" ? "Regional sourcing" : "Источники кандидатов" },
        ]}
        actions={<LocaleSwitcher />}
      />
      <div className="flex-1 overflow-y-auto px-7 py-8">
        <SourcingWorkspace
          access={access}
          initialIntegrations={integrations.data.items}
          sourceIntegrationCursor={integrations.data.next_cursor}
          providerConnections={providerConnections.data.items}
          initialRuns={runs.data.items}
          positions={positions.data}
          initialAnalytics={analytics.data}
          initialPositionId={initialPositionId}
          initialReferrals={referrals.data}
          loadFailures={failures}
          publicApiBaseUrl={publicApiBaseUrl}
        />
      </div>
    </>
  );
}
