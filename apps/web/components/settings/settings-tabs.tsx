"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  CalendarDays,
  Check,
  ClipboardCheck,
  Copy,
  FileSignature,
  KeyRound,
  Mail,
  Palette,
  Plus,
  Settings as SettingsIcon,
  ShieldCheck,
  Trash2,
  Users,
  Waypoints,
  Workflow,
} from "lucide-react";

import {
  changePasswordAction,
  createUserInvitationAction,
  createUserPasswordResetAction,
  deleteUserAction,
  revokeUserAccessTokenAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { BrandingTab } from "@/components/settings/branding-tab";
import { CareersIntegrationTab } from "@/components/settings/careers-integration-tab";
import { CommunicationsSettingsTab } from "@/components/settings/communications-settings-tab";
import { SchedulingSettingsTab } from "@/components/settings/scheduling-settings-tab";
import { ScorecardTemplatesTab } from "@/components/settings/scorecard-templates-tab";
import { WorkflowAutomationTab } from "@/components/settings/workflow-automation-tab";
import { OfferTemplatesTab } from "@/components/settings/offer-templates-tab";
import { SecuritySettingsTab } from "@/components/settings/security-settings-tab";
import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { ThemeToggle } from "@/components/app/theme-toggle";
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
  UserAccessCreated,
  WorkflowRule,
  WorkflowRunPage,
} from "@/lib/types";
import type { SettingsAccess } from "@/lib/permissions";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "sonner";
import type { components } from "@/lib/generated/api";

type RefreshSession = components["schemas"]["RefreshSessionOut"];

/** @ui-only Settings workspace tab state. */
type Tab =
  | "profile"
  | "security"
  | "calendar"
  | "team"
  | "branding"
  | "careers"
  | "communications"
  | "scorecards"
  | "workflows"
  | "offers";

export function SettingsTabs({
  me,
  access,
  users,
  usersError,
  organization,
  initialLogoUrl,
  careersApiUrl,
  communicationAccounts,
  communicationTemplates,
  communicationsLoadFailed,
  communicationsWebhookBaseUrl,
  positions,
  scorecardTemplates,
  scorecardsLoadFailed,
  workflowRules,
  workflowRuns,
  workflowRulesLoadFailed,
  workflowRunsLoadFailed,
  offerTemplates,
  offerTemplatesLoadFailed,
  availability,
  availabilityBlocks,
  calendarProviders,
  availabilityLoadFailed,
  blocksLoadFailed,
  providersLoadFailed,
  securitySessions,
  securityLoadFailed,
  adminMfaRequired,
  mfaEnabled,
}: {
  me: AppUser;
  access: SettingsAccess;
  users: AppUser[];
  usersError: string | null;
  organization: Organization | null;
  initialLogoUrl: string | null;
  careersApiUrl: string | null;
  communicationAccounts: CommunicationAccount[];
  communicationTemplates: CommunicationTemplate[];
  communicationsLoadFailed: boolean;
  communicationsWebhookBaseUrl: string;
  positions: Position[];
  scorecardTemplates: ScorecardTemplate[];
  scorecardsLoadFailed: boolean;
  workflowRules: WorkflowRule[];
  workflowRuns: WorkflowRunPage;
  workflowRulesLoadFailed: boolean;
  workflowRunsLoadFailed: boolean;
  offerTemplates: OfferTemplate[];
  offerTemplatesLoadFailed: boolean;
  availability: Availability;
  availabilityBlocks: AvailabilityBlock[];
  calendarProviders: CalendarProvider[];
  availabilityLoadFailed: boolean;
  blocksLoadFailed: boolean;
  providersLoadFailed: boolean;
  securitySessions: RefreshSession[];
  securityLoadFailed: boolean;
  adminMfaRequired: boolean;
  mfaEnabled: boolean;
}) {
  const { m, locale } = useI18n();
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedTab = searchParams.get("tab") as Tab | null;
  const allowedTabs = useCallback(
    (value: string | null): value is Tab => {
      if (value === "profile" || value === "security") return true;
      if (value === "calendar") return access.viewCalendar;
      if (value === "team") return access.viewTeam;
      if (value === "branding") return access.viewBranding;
      if (value === "careers") return access.viewCareers;
      if (value === "communications") {
        return (
          access.viewCommunicationAccounts || access.viewCommunicationTemplates
        );
      }
      if (value === "scorecards") return access.viewScorecards;
      if (value === "workflows") return access.viewWorkflows;
      if (value === "offers") return access.viewOffers;
      return false;
    },
    [access],
  );
  const [tab, setTab] = useState<Tab>(() =>
    allowedTabs(requestedTab) ? requestedTab : "profile",
  );

  useEffect(() => {
    setTab(allowedTabs(requestedTab) ? requestedTab : "profile");
  }, [allowedTabs, requestedTab]);

  function selectTab(next: Tab) {
    setTab(next);
    const params = new URLSearchParams(searchParams.toString());
    params.set("tab", next);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  return (
    <>
      <div
        role="tablist"
        aria-label={locale === "ru" ? "Разделы настроек" : "Settings sections"}
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          const tabs = Array.from(
            event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
          );
          const current = tabs.indexOf(document.activeElement as HTMLButtonElement);
          if (current < 0) return;
          event.preventDefault();
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? tabs.length - 1
                : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
          tabs[next]?.focus();
          tabs[next]?.click();
        }}
        className="mb-6 flex items-center gap-1 overflow-x-auto border-b border-border"
      >
        <TabBtn
          tab="profile"
          active={tab === "profile"}
          onClick={() => selectTab("profile")}
          icon={<SettingsIcon className="h-3.5 w-3.5" />}
        >
          {m.settings.tabProfile}
        </TabBtn>
        <TabBtn
          tab="security"
          active={tab === "security"}
          onClick={() => selectTab("security")}
          icon={<ShieldCheck className="h-3.5 w-3.5" />}
        >
          {locale === "ru" ? "Безопасность" : "Security"}
        </TabBtn>
        {access.viewCalendar ? (
          <TabBtn
            tab="calendar"
            active={tab === "calendar"}
            onClick={() => selectTab("calendar")}
            icon={<CalendarDays className="h-3.5 w-3.5" />}
          >
            {m.scheduling.settings.tab}
          </TabBtn>
        ) : null}
        {access.viewCommunicationAccounts || access.viewCommunicationTemplates ? (
          <TabBtn
            tab="communications"
            active={tab === "communications"}
            onClick={() => selectTab("communications")}
            icon={<Mail className="h-3.5 w-3.5" />}
          >
            {m.communications.settingsTab}
          </TabBtn>
        ) : null}
        {access.viewScorecards ? (
          <TabBtn
            tab="scorecards"
            active={tab === "scorecards"}
            onClick={() => selectTab("scorecards")}
            icon={<ClipboardCheck className="h-3.5 w-3.5" />}
          >
            {m.scorecards.settingsTab}
          </TabBtn>
        ) : null}
        {access.viewWorkflows ? (
          <TabBtn
            tab="workflows"
            active={tab === "workflows"}
            onClick={() => selectTab("workflows")}
            icon={<Workflow className="h-3.5 w-3.5" />}
          >
            {m.workflows.settingsTab}
          </TabBtn>
        ) : null}
        {access.viewOffers ? (
          <TabBtn
            tab="offers"
            active={tab === "offers"}
            onClick={() => selectTab("offers")}
            icon={<FileSignature className="h-3.5 w-3.5" />}
          >
            {m.offers.settingsTab}
          </TabBtn>
        ) : null}
        {access.viewTeam ? (
          <TabBtn
            tab="team"
            active={tab === "team"}
            onClick={() => selectTab("team")}
            icon={<Users className="h-3.5 w-3.5" />}
          >
            {m.settings.tabTeam}{" "}
            <span className="ml-1 font-mono text-[10px] text-text-faint tabular-nums">
              {users.length}
            </span>
          </TabBtn>
        ) : null}
        {access.viewBranding ? (
          <TabBtn
            tab="branding"
            active={tab === "branding"}
            onClick={() => selectTab("branding")}
            icon={<Palette className="h-3.5 w-3.5" />}
          >
            {m.settings.tabBranding}
          </TabBtn>
        ) : null}
        {access.viewCareers ? (
          <TabBtn
            tab="careers"
            active={tab === "careers"}
            onClick={() => selectTab("careers")}
            icon={<Waypoints className="h-3.5 w-3.5" />}
          >
            {m.settings.tabCareers}
          </TabBtn>
        ) : null}
      </div>

      <div
        id={`settings-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`settings-tab-${tab}`}
        tabIndex={0}
        className="outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
      {tab === "profile" ? (
        <ProfileTab me={me} canManage={access.manageProfile} />
      ) : tab === "security" ? (
        <SecuritySettingsTab
          sessions={securitySessions}
          loadFailed={securityLoadFailed}
          adminMfaRequired={adminMfaRequired}
          mfaEnabled={mfaEnabled}
        />
      ) : tab === "calendar" ? (
        <SchedulingSettingsTab
          initialAvailability={availability}
          initialBlocks={availabilityBlocks}
          initialProviders={calendarProviders}
          availabilityLoadFailed={availabilityLoadFailed}
          blocksLoadFailed={blocksLoadFailed}
          providersLoadFailed={providersLoadFailed}
          canManage={access.manageCalendar}
        />
      ) : tab === "team" ? (
        <TeamTab
          users={users}
          error={usersError}
          currentUserId={me.id}
          canManage={access.manageTeam}
        />
      ) : tab === "branding" ? (
        <BrandingTab
          organization={organization}
          initialLogoUrl={initialLogoUrl}
          canManage={access.manageBranding}
        />
      ) : tab === "careers" ? (
        <CareersIntegrationTab
          organization={organization}
          apiUrl={careersApiUrl}
        />
      ) : tab === "scorecards" ? (
        <ScorecardTemplatesTab
          initialTemplates={scorecardTemplates}
          positions={positions}
          initialLoadFailed={scorecardsLoadFailed}
          canManage={access.manageScorecards}
        />
      ) : tab === "workflows" ? (
        <WorkflowAutomationTab
          initialRules={workflowRules}
          initialRuns={workflowRuns}
          positions={positions}
          users={users}
          communicationTemplates={communicationTemplates}
          rulesLoadFailed={workflowRulesLoadFailed}
          runsLoadFailed={workflowRunsLoadFailed}
          canManage={access.manageWorkflows}
          canExecute={access.executeWorkflows}
        />
      ) : tab === "offers" ? (
        <OfferTemplatesTab
          initialTemplates={offerTemplates}
          initialLoadFailed={offerTemplatesLoadFailed}
          canManage={access.manageOffers}
        />
      ) : (
        <CommunicationsSettingsTab
          initialAccounts={communicationAccounts}
          initialTemplates={communicationTemplates}
          initialLoadFailed={communicationsLoadFailed}
          webhookBaseUrl={communicationsWebhookBaseUrl}
          access={{
            viewAccounts: access.viewCommunicationAccounts,
            manageAccounts: access.manageCommunicationAccounts,
            rotateCredentials: access.rotateCommunicationCredentials,
            viewTemplates: access.viewCommunicationTemplates,
            manageTemplates: access.manageCommunicationTemplates,
          }}
        />
      )}
      </div>
    </>
  );
}

function TabBtn({
  tab,
  active,
  onClick,
  icon,
  children,
}: {
  tab: Tab;
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      id={`settings-tab-${tab}`}
      role="tab"
      aria-selected={active}
      aria-controls={`settings-panel-${tab}`}
      tabIndex={active ? 0 : -1}
      onClick={onClick}
      className={cn(
        "-mb-px flex items-center gap-1.5 border-b px-3.5 py-3 text-[12px] font-medium transition-colors",
        active
          ? "border-text text-text"
          : "border-transparent text-text-muted hover:text-text",
      )}
    >
      {icon}
      {children}
    </button>
  );
}

// ---- Profile tab ----

function userRoleLabel(
  role: string,
  locale: "ru" | "en",
  copy: {
    roleAdmin: string;
    roleRecruiter: string;
    roleHiringManager: string;
  },
): string {
  switch (role) {
    case "admin":
      return copy.roleAdmin;
    case "hiring_manager":
      return copy.roleHiringManager;
    case "interviewer":
      return locale === "ru" ? "Интервьюер" : "Interviewer";
    case "restricted":
      return locale === "ru" ? "Ограниченный" : "Restricted";
    default:
      return copy.roleRecruiter;
  }
}

function ProfileTab({ me, canManage }: { me: AppUser; canManage: boolean }) {
  const { m, locale } = useI18n();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [pending, start] = useTransition();
  const roleLabel = userRoleLabel(me.role, locale, m.settings);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!current || !next) return;
    start(async () => {
      const res = await changePasswordAction(current, next);
      if (res.ok) {
        toast.success(m.settings.passwordChangedToast);
        setCurrent("");
        setNext("");
      } else {
        toast.error(
          res.error.includes("401") ? m.settings.passwordWrongToast : res.error,
        );
      }
    });
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        {/* Account info */}
        <section className="reveal rounded-xl border border-border bg-bg-elevated p-6">
          <h2 className="mb-4 text-[12px] font-semibold uppercase tracking-[0.08em] text-text-muted">
            {m.settings.profileSection}
          </h2>
          <dl className="flex flex-col gap-3 text-[13px]">
            <Row label={m.settings.emailLabel} value={me.email} mono />
            <Row label={m.settings.roleLabel} value={roleLabel} />
            <Row
              label={m.settings.localeLabel}
              value={me.locale.toUpperCase()}
              mono
            />
            {me.created_at ? (
              <Row
                label={m.settings.createdLabel}
                value={new Date(me.created_at).toLocaleDateString()}
                mono
              />
            ) : null}
          </dl>
        </section>

        {canManage ? (
          <section
            className="reveal rounded-xl border border-border bg-bg-elevated p-6"
            style={{ animationDelay: "40ms" }}
          >
            <h2 className="mb-4 text-[12px] font-semibold uppercase tracking-[0.08em] text-text-muted">
              {m.settings.passwordSection}
            </h2>
            <form onSubmit={submit} className="flex flex-col gap-3">
            <Field label={m.settings.currentPassword}>
              <input
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                required
                className="rounded-[6px] border border-border bg-bg px-3 py-2.5 text-[13px] outline-none transition-colors focus:border-border-strong"
              />
            </Field>
            <Field label={m.settings.newPassword}>
              <input
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                required
                minLength={8}
                className="rounded-[6px] border border-border bg-bg px-3 py-2.5 text-[13px] outline-none transition-colors focus:border-border-strong"
              />
            </Field>
            <button
              type="submit"
              disabled={pending || !current || next.length < 8}
              className="press mt-1 inline-flex w-fit items-center gap-1.5 rounded-[6px] bg-text px-3 py-2 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {pending
                ? m.settings.changingPassword
                : m.settings.changePassword}
            </button>
            </form>
          </section>
        ) : null}
      </div>

      {/* Preferences — surfaces appearance + locale in one place. */}
      <section
        className="reveal relative overflow-hidden rounded-xl border border-border bg-bg-elevated p-6"
        style={{ animationDelay: "80ms" }}
      >
        {/* Soft accent gradient on the left edge — gives the card a quiet
            "this is a control surface" tone vs. the read-only sections above. */}
        <span
          aria-hidden
          className="pointer-events-none absolute inset-y-0 left-0 w-px bg-gradient-to-b from-transparent via-accent to-transparent opacity-50"
        />
        <div className="mb-4 flex items-baseline justify-between gap-3">
          <h2 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-text-muted">
            {m.settings.preferencesSection}
          </h2>
          <span className="text-[11px] text-text-faint">
            {m.settings.preferencesHint}
          </span>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <PreferenceRow label={m.settings.preferencesTheme}>
            <ThemeToggle />
          </PreferenceRow>
          <PreferenceRow label={m.settings.preferencesLanguage}>
            <LocaleSwitcher />
          </PreferenceRow>
        </div>
      </section>
    </div>
  );
}

function PreferenceRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-[8px] border border-border/60 bg-bg px-3.5 py-3">
      <span className="text-[12.5px] font-medium text-text">{label}</span>
      {children}
    </div>
  );
}

function Row({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-2 last:border-b-0 last:pb-0">
      <dt className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
        {label}
      </dt>
      <dd className={cn("text-text", mono && "font-mono text-[12.5px]")}>
        {value}
      </dd>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] font-medium text-text-muted">{label}</span>
      {children}
    </label>
  );
}

// ---- Team tab ----

function TeamTab({
  users,
  error,
  currentUserId,
  canManage,
}: {
  users: AppUser[];
  error: string | null;
  currentUserId: string;
  canManage: boolean;
}) {
  const { m } = useI18n();
  const [inviteOpen, setInviteOpen] = useState(false);
  const [resetUser, setResetUser] = useState<AppUser | null>(null);
  const [confirmDeleteUserId, setConfirmDeleteUserId] = useState<string | null>(null);
  const [deleting, startDelete] = useTransition();

  function doDelete() {
    if (!canManage || !confirmDeleteUserId) return;
    startDelete(async () => {
      const res = await deleteUserAction(confirmDeleteUserId);
      if (res.ok) {
        toast.success(m.settings.deleteUserToast);
        setConfirmDeleteUserId(null);
      } else {
        toast.error(res.error);
      }
    });
  }

  if (error) {
    return (
      <div className="rounded-lg border border-border bg-bg-elevated p-5 text-[13px] text-score-low">
        {error}
      </div>
    );
  }

  return (
    <>
      <div className="mb-4 flex items-end justify-between">
        <p className="text-[13px] text-text-muted">
          {m.settings.teamSummary(users.length)}
        </p>
        {canManage ? (
          <button
            type="button"
            onClick={() => setInviteOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90"
          >
            <Plus className="h-3.5 w-3.5" />
            {m.settings.inviteUserBtn}
          </button>
        ) : null}
      </div>

      <ul className="overflow-hidden rounded-xl border border-border bg-bg-elevated">
        {users.map((u, i) => (
          <UserRow
            key={u.id}
            user={u}
            isLast={i === users.length - 1}
            isMe={u.id === currentUserId}
            canManage={canManage}
            onReset={() => setResetUser(u)}
            onDelete={() => setConfirmDeleteUserId(u.id)}
          />
        ))}
      </ul>

      {canManage ? (
        <InviteDialog open={inviteOpen} onOpenChange={setInviteOpen} />
      ) : null}
      {canManage ? (
        <PasswordResetDialog
          user={resetUser}
          onOpenChange={(open) => {
            if (!open) setResetUser(null);
          }}
        />
      ) : null}

      {canManage ? <Dialog
        open={!!confirmDeleteUserId}
        onOpenChange={(o) => !o && setConfirmDeleteUserId(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="text-[15px]">
              {m.settings.confirmDeleteUser}
            </DialogTitle>
            <DialogDescription className="text-[12px] text-text-muted">
              {m.settings.confirmDeleteUserDesc}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <button
              type="button"
              onClick={() => setConfirmDeleteUserId(null)}
              className="rounded-[6px] px-3 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
            >
              {m.position.cancel}
            </button>
            <button
              type="button"
              onClick={doDelete}
              disabled={deleting}
              className="inline-flex items-center gap-1.5 rounded-[6px] px-3 py-1.5 text-[12px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
              style={{ background: "var(--score-low)" }}
            >
              <Trash2 className="h-3.5 w-3.5" />
              {m.settings.deleteUserAction}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog> : null}
    </>
  );
}

function UserRow({
  user,
  isLast,
  isMe,
  canManage,
  onReset,
  onDelete,
}: {
  user: AppUser;
  isLast: boolean;
  isMe: boolean;
  canManage: boolean;
  onReset: () => void;
  onDelete: () => void;
}) {
  const { m, locale } = useI18n();
  const roleLabel = userRoleLabel(user.role, locale, m.settings);
  const initial = (user.email[0] ?? "?").toUpperCase();

  return (
    <li
      className={cn(
        "flex items-center gap-4 px-5 py-3.5",
        !isLast && "border-b border-border",
      )}
    >
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-[oklch(0.5_0.05_162)] to-[oklch(0.35_0.08_200)] font-semibold text-[12px] text-white">
        {initial}
      </span>
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-mono text-[12.5px] text-text">
          {user.email}
        </span>
        {user.created_at ? (
          <span className="font-mono text-[10px] text-text-faint">
            {new Date(user.created_at).toLocaleDateString()}
          </span>
        ) : null}
      </div>
      <span className="rounded-[4px] border border-border bg-bg px-2 py-1 font-mono text-[10px] uppercase tracking-[0.06em] text-text-muted">
        {roleLabel}
      </span>
      {user.role === "admin" ? (
        <ShieldCheck className="h-3.5 w-3.5 text-primary" />
      ) : (
        <span className="w-3.5" />
      )}
      {isMe ? (
        <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-text-faint">
          {m.settings.you}
        </span>
      ) : canManage ? (
        <span className="flex items-center gap-1">
          {user.is_active !== false ? (
            <button
              type="button"
              onClick={onReset}
              title={locale === "ru" ? "Создать ссылку сброса пароля" : "Create password reset link"}
              className="flex h-7 w-7 items-center justify-center rounded-[6px] text-text-faint transition-colors hover:bg-bg-hover hover:text-text"
            >
              <KeyRound className="h-3.5 w-3.5" />
            </button>
          ) : null}
          <button
            type="button"
            onClick={onDelete}
            title={m.settings.deleteUserAction}
            className="flex h-7 w-7 items-center justify-center rounded-[6px] text-text-faint transition-colors hover:bg-bg-hover hover:text-score-low"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </span>
      ) : (
        <span className="w-7" />
      )}
    </li>
  );
}

function InviteDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const { m, locale } = useI18n();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("recruiter");
  const [expiresInDays, setExpiresInDays] = useState("7");
  const [access, setAccess] = useState<UserAccessCreated | null>(null);
  const [pending, start] = useTransition();

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!email) return;
    start(async () => {
      const res = await createUserInvitationAction({
        email,
        role,
        locale,
        expires_in_days: Number(expiresInDays),
      });
      if (res.ok) {
        toast.success(m.settings.userCreatedToast);
        setAccess(res.data);
      } else if (res.error.includes("409")) {
        toast.error(m.settings.userExistsToast);
      } else {
        toast.error(res.error);
      }
    });
  }

  function changeOpen(next: boolean) {
    if (!next) {
      setEmail("");
      setRole("recruiter");
      setExpiresInDays("7");
      setAccess(null);
    }
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-[15px]">
            {m.settings.inviteDialogTitle}
          </DialogTitle>
          <DialogDescription className="text-[12px] text-text-muted">
            {m.settings.inviteDialogDesc}
          </DialogDescription>
        </DialogHeader>
        {access ? (
          <AccessLinkPanel
            access={access}
            onRevoked={() => {
              setAccess(null);
              changeOpen(false);
            }}
          />
        ) : <form onSubmit={submit} className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium text-text-muted">
              <Mail className="mr-1 inline h-3 w-3" /> Email
            </span>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="rounded-[6px] border border-border bg-bg px-3 py-2.5 text-[13px] outline-none transition-colors focus:border-border-strong"
            />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium text-text-muted">
              {m.settings.roleLabel}
            </span>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="rounded-[6px] border border-border bg-bg px-3 py-2.5 text-[13px] outline-none transition-colors focus:border-border-strong"
            >
              <option value="recruiter">{m.settings.roleRecruiter}</option>
              <option value="hiring_manager">
                {m.settings.roleHiringManager}
              </option>
              <option value="interviewer">
                {locale === "ru" ? "Интервьюер" : "Interviewer"}
              </option>
              <option value="restricted">
                {locale === "ru" ? "Ограниченный" : "Restricted"}
              </option>
              <option value="admin">{m.settings.roleAdmin}</option>
            </select>
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium text-text-muted">
              {locale === "ru" ? "Срок действия ссылки" : "Link expiry"}
            </span>
            <select
              value={expiresInDays}
              onChange={(event) => setExpiresInDays(event.target.value)}
              className="rounded-[6px] border border-border bg-bg px-3 py-2.5 text-[13px] outline-none transition-colors focus:border-border-strong"
            >
              <option value="1">{locale === "ru" ? "1 день" : "1 day"}</option>
              <option value="7">{locale === "ru" ? "7 дней" : "7 days"}</option>
              <option value="14">{locale === "ru" ? "14 дней" : "14 days"}</option>
              <option value="30">{locale === "ru" ? "30 дней" : "30 days"}</option>
            </select>
          </label>
          <DialogFooter className="gap-2 pt-2">
            <button
              type="button"
              onClick={() => changeOpen(false)}
              className="rounded-[6px] px-3 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
            >
              {m.position.cancel}
            </button>
            <button
              type="submit"
              disabled={pending || !email}
              className="inline-flex items-center rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {pending ? m.settings.inviteCreating : m.settings.inviteCreate}
            </button>
          </DialogFooter>
        </form>}
      </DialogContent>
    </Dialog>
  );
}

function PasswordResetDialog({
  user,
  onOpenChange,
}: {
  user: AppUser | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { locale } = useI18n();
  const [access, setAccess] = useState<UserAccessCreated | null>(null);
  const [pending, start] = useTransition();

  function changeOpen(open: boolean) {
    if (!open) setAccess(null);
    onOpenChange(open);
  }

  function createReset() {
    if (!user || pending) return;
    start(async () => {
      const result = await createUserPasswordResetAction(user.id);
      if (!result.ok) {
        toast.error(result.error);
        return;
      }
      setAccess(result.data);
    });
  }

  return (
    <Dialog open={user !== null} onOpenChange={changeOpen}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-[15px]">
            {locale === "ru" ? "Сброс пароля" : "Password reset"}
          </DialogTitle>
          <DialogDescription className="text-[12px] text-text-muted">
            {locale === "ru"
              ? `Создайте одноразовую ссылку для ${user?.email ?? ""}. Она заменит ранее выданную ссылку.`
              : `Create a one-time link for ${user?.email ?? ""}. It replaces any previously issued link.`}
          </DialogDescription>
        </DialogHeader>
        {access ? (
          <AccessLinkPanel
            access={access}
            onRevoked={() => changeOpen(false)}
          />
        ) : (
          <DialogFooter className="gap-2">
            <button
              type="button"
              onClick={() => changeOpen(false)}
              className="min-h-9 rounded-[6px] px-3 py-2 text-[12px] font-medium text-text-muted hover:bg-bg-hover"
            >
              {locale === "ru" ? "Отмена" : "Cancel"}
            </button>
            <button
              type="button"
              onClick={createReset}
              disabled={pending || !user}
              className="inline-flex min-h-9 items-center gap-2 rounded-[6px] bg-text px-3 py-2 text-[12px] font-medium text-bg disabled:opacity-50"
            >
              <KeyRound className="h-3.5 w-3.5" />
              {pending
                ? locale === "ru" ? "Создаём…" : "Creating…"
                : locale === "ru" ? "Создать ссылку" : "Create link"}
            </button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}

function AccessLinkPanel({
  access,
  onRevoked,
}: {
  access: UserAccessCreated;
  onRevoked: () => void;
}) {
  const { locale } = useI18n();
  const [copied, setCopied] = useState(false);
  const [pending, start] = useTransition();

  function copyLink() {
    void navigator.clipboard.writeText(access.access_url).then(
      () => setCopied(true),
      () => toast.error(locale === "ru" ? "Не удалось скопировать" : "Could not copy"),
    );
  }

  function revoke() {
    start(async () => {
      const result = await revokeUserAccessTokenAction(access.id);
      if (!result.ok) {
        toast.error(result.error);
        return;
      }
      toast.success(locale === "ru" ? "Ссылка отозвана" : "Link revoked");
      onRevoked();
    });
  }

  return (
    <div className="space-y-4">
      <div
        role="status"
        className="rounded-lg border border-score-mid/30 bg-score-mid/5 p-3 text-[12px]"
      >
        <p className="font-medium text-score-mid">
          {locale === "ru"
            ? "Ссылка показывается только сейчас"
            : "This link is shown only now"}
        </p>
        <p className="mt-1 text-text-muted">
          {access.delivery_enqueued
            ? locale === "ru" ? "Письмо поставлено в очередь." : "Email delivery was queued."
            : locale === "ru" ? "Email-провайдер недоступен — скопируйте ссылку вручную." : "No email provider is available—copy the link manually."}
        </p>
        <p className="mt-1 text-text-faint">
          {locale === "ru" ? "Истекает" : "Expires"}: {new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", { dateStyle: "medium", timeStyle: "short" }).format(new Date(access.expires_at))}
        </p>
      </div>
      <label className="flex flex-col gap-1.5">
        <span className="text-[11px] font-medium text-text-muted">
          {locale === "ru" ? "Одноразовая ссылка" : "One-time link"}
        </span>
        <input
          readOnly
          value={access.access_url}
          className="min-h-10 w-full rounded-[6px] border border-border bg-bg px-3 py-2 font-mono text-[11px] outline-none focus:border-border-strong"
        />
      </label>
      <div className="flex flex-wrap justify-end gap-2">
        <button
          type="button"
          onClick={revoke}
          disabled={pending}
          className="min-h-9 rounded-[6px] px-3 py-2 text-[12px] font-medium text-score-low hover:bg-score-low/10 disabled:opacity-50"
        >
          {locale === "ru" ? "Отозвать" : "Revoke"}
        </button>
        <button
          type="button"
          onClick={copyLink}
          className="inline-flex min-h-9 items-center gap-2 rounded-[6px] bg-text px-3 py-2 text-[12px] font-medium text-bg"
        >
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied
            ? locale === "ru" ? "Скопировано" : "Copied"
            : locale === "ru" ? "Копировать" : "Copy link"}
        </button>
      </div>
      <span aria-live="polite" className="sr-only">
        {copied ? (locale === "ru" ? "Ссылка скопирована" : "Link copied") : ""}
      </span>
    </div>
  );
}
