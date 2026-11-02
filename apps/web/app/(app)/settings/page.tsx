import { auth } from "@/auth";
import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { SettingsTabs } from "@/components/settings/settings-tabs";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { getCurrentPermissions } from "@/lib/current-permissions";
import type { components } from "@/lib/generated/api";
import { getMessages } from "@/lib/i18n";
import { getSettingsAccess, NO_PERMISSIONS } from "@/lib/permissions";
import { publicBrandingUrl } from "@/lib/public-branding";
import type {
  AppUser,
  Availability,
  AvailabilityBlock,
  CalendarProvider,
  CommunicationAccount,
  CommunicationTemplate,
  Organization,
  OfferTemplate,
  Position,
  ScorecardTemplate,
  WorkflowRule,
  WorkflowRunPage,
} from "@/lib/types";

type AuthMe = components["schemas"]["MeOut"];
type RefreshSession = components["schemas"]["RefreshSessionOut"];

export const dynamic = "force-dynamic";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
const PUBLIC_API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? API_BASE_URL;

function rethrowRedirect(result: PromiseSettledResult<unknown>) {
  if (
    result.status === "rejected" &&
    (result.reason as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")
  ) {
    throw result.reason;
  }
}

export default async function SettingsPage() {
  const m = await getMessages();
  const session = await auth();
  const effective = await getCurrentPermissions();
  const access = getSettingsAccess(effective?.permissions ?? NO_PERMISSIONS);

  // Resolve policy before protected requests. Skipped requests use inert
  // values so a denied tab never probes a forbidden API.
  const [
    securityMeResult,
    securitySessionsResult,
    usersResult,
    organizationResult,
    accountsResult,
    templatesResult,
    positionsResult,
    scorecardsResult,
    workflowRulesResult,
    workflowRunsResult,
    offerTemplatesResult,
    availabilityResult,
    blocksResult,
    providersResult,
  ] = await Promise.allSettled([
    apiGet<AuthMe>("/auth/me"),
    apiGetAll<RefreshSession>("/auth/sessions"),
    access.readTeam
      ? apiGetAll<AppUser>("/users")
      : Promise.resolve<AppUser[]>([]),
    access.readBranding
      ? apiGet<Organization>("/organizations/me")
      : Promise.resolve<Organization | null>(null),
    access.readCommunicationAccounts
      ? apiGetAll<CommunicationAccount>("/communications/accounts")
      : Promise.resolve<CommunicationAccount[]>([]),
    access.readCommunicationTemplates
      ? apiGetAll<CommunicationTemplate>(
          "/communications/templates?include_inactive=true",
        )
      : Promise.resolve<CommunicationTemplate[]>([]),
    access.viewPositions && (access.viewScorecards || access.viewWorkflows)
      ? apiGetAll<Position>("/positions")
      : Promise.resolve<Position[]>([]),
    access.readScorecards
      ? apiGetAll<ScorecardTemplate>(
          "/scorecard-templates?include_inactive=true",
        )
      : Promise.resolve<ScorecardTemplate[]>([]),
    access.readWorkflows
      ? apiGetAll<WorkflowRule>("/workflows/rules?include_inactive=true")
      : Promise.resolve<WorkflowRule[]>([]),
    access.readWorkflows
      ? apiGet<WorkflowRunPage>("/workflows/runs?limit=30")
      : Promise.resolve<WorkflowRunPage>({ items: [], next_cursor: null }),
    access.readOffers
      ? apiGetAll<OfferTemplate>("/offer-templates")
      : Promise.resolve<OfferTemplate[]>([]),
    access.readCalendar
      ? apiGet<Availability>("/calendar/availability/me")
      : Promise.resolve<Availability>({ timezone: "UTC", intervals: [] }),
    access.readCalendar
      ? apiGetAll<AvailabilityBlock>("/calendar/availability/blocks")
      : Promise.resolve<AvailabilityBlock[]>([]),
    access.readCalendar
      ? apiGetAll<CalendarProvider>("/calendar/providers")
      : Promise.resolve<CalendarProvider[]>([]),
  ]);

  for (const result of [
    securityMeResult,
    securitySessionsResult,
    usersResult,
    organizationResult,
    accountsResult,
    templatesResult,
    positionsResult,
    scorecardsResult,
    workflowRulesResult,
    workflowRunsResult,
    offerTemplatesResult,
    availabilityResult,
    blocksResult,
    providersResult,
  ]) {
    rethrowRedirect(result);
  }

  const users = usersResult.status === "fulfilled" ? usersResult.value : [];
  const sessionOrganization = session?.user?.organization;
  const organization =
    organizationResult.status === "fulfilled" && organizationResult.value
      ? organizationResult.value
      : access.manageBranding && sessionOrganization
        ? {
            ...sessionOrganization,
            logo_path: null,
            accent_color: null,
          }
        : null;
  const communicationAccounts =
    accountsResult.status === "fulfilled" ? accountsResult.value : [];
  const communicationTemplates =
    templatesResult.status === "fulfilled" ? templatesResult.value : [];
  const positions =
    positionsResult.status === "fulfilled" ? positionsResult.value : [];
  const scorecardTemplates =
    scorecardsResult.status === "fulfilled" ? scorecardsResult.value : [];
  const workflowRules =
    workflowRulesResult.status === "fulfilled" ? workflowRulesResult.value : [];
  const workflowRuns =
    workflowRunsResult.status === "fulfilled"
      ? workflowRunsResult.value
      : { items: [], next_cursor: null };
  const offerTemplates =
    offerTemplatesResult.status === "fulfilled"
      ? offerTemplatesResult.value
      : [];
  const availability =
    availabilityResult.status === "fulfilled"
      ? availabilityResult.value
      : { timezone: "UTC", intervals: [] };
  const availabilityBlocks =
    blocksResult.status === "fulfilled" ? blocksResult.value : [];
  const calendarProviders =
    providersResult.status === "fulfilled" ? providersResult.value : [];
  const securitySessions =
    securitySessionsResult.status === "fulfilled"
      ? securitySessionsResult.value
      : [];
  const securityLoadFailed =
    securityMeResult.status === "rejected" ||
    securitySessionsResult.status === "rejected";
  const mfaEnabled =
    securityMeResult.status === "fulfilled"
      ? securityMeResult.value.mfa_enabled
      : false;
  // Admin MFA is an authentication identity invariant, not authorization for
  // an unrelated resource.
  const adminMfaRequired =
    securityMeResult.status === "fulfilled" &&
    securityMeResult.value.role === "admin";
  const usersError =
    access.readTeam && usersResult.status === "rejected"
      ? (usersResult.reason as Error).message
      : null;
  const communicationsLoadFailed =
    (access.readCommunicationAccounts && accountsResult.status === "rejected") ||
    (access.readCommunicationTemplates && templatesResult.status === "rejected");

  const role = session?.user?.role ?? effective?.role ?? "restricted";
  const meEmail = session?.user?.email ?? "";
  const meId = (session?.user as { id?: string } | undefined)?.id ?? "";
  const me: AppUser =
    users.find((user) => user.email === meEmail) ?? {
      id: meId,
      email: meEmail,
      role,
      locale: session?.user?.locale ?? "ru",
      created_at: null,
    };

  return (
    <>
      <Topbar
        crumbs={[{ label: m.settings.crumb }]}
        actions={<LocaleSwitcher />}
      />
      <div className="flex-1 overflow-y-auto overflow-x-hidden px-7 py-8">
        <header className="mb-7">
          <h1 className="text-[24px] font-semibold tracking-[-0.025em]">
            {m.settings.title}
          </h1>
        </header>
        <SettingsTabs
          me={me}
          access={access}
          users={users}
          usersError={usersError}
          organization={organization}
          initialLogoUrl={
            organization ? publicBrandingUrl(organization.logo_path) : null
          }
          careersApiUrl={
            organization
              ? `${PUBLIC_API_BASE_URL.replace(/\/$/, "")}/public/v1/careers/${encodeURIComponent(organization.slug)}/positions`
              : null
          }
          communicationAccounts={communicationAccounts}
          communicationTemplates={communicationTemplates}
          communicationsLoadFailed={communicationsLoadFailed}
          communicationsWebhookBaseUrl={PUBLIC_API_BASE_URL}
          positions={positions}
          scorecardTemplates={scorecardTemplates}
          scorecardsLoadFailed={
            access.readScorecards && scorecardsResult.status === "rejected"
          }
          workflowRules={workflowRules}
          workflowRuns={workflowRuns}
          workflowRulesLoadFailed={
            access.readWorkflows && workflowRulesResult.status === "rejected"
          }
          workflowRunsLoadFailed={
            access.readWorkflows && workflowRunsResult.status === "rejected"
          }
          offerTemplates={offerTemplates}
          offerTemplatesLoadFailed={
            access.readOffers && offerTemplatesResult.status === "rejected"
          }
          availability={availability}
          availabilityBlocks={availabilityBlocks}
          calendarProviders={calendarProviders}
          availabilityLoadFailed={
            access.readCalendar && availabilityResult.status === "rejected"
          }
          blocksLoadFailed={
            access.readCalendar && blocksResult.status === "rejected"
          }
          providersLoadFailed={
            access.readCalendar && providersResult.status === "rejected"
          }
          securitySessions={securitySessions}
          securityLoadFailed={securityLoadFailed}
          adminMfaRequired={adminMfaRequired}
          mfaEnabled={mfaEnabled}
        />
      </div>
    </>
  );
}
