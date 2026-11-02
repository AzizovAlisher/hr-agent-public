import {
  BarChart3,
  BriefcaseBusiness,
  ClipboardCheck,
  FolderHeart,
  LayoutGrid,
  ListTodo,
  MessageSquare,
  PlugZap,
  RadioTower,
  Settings,
  ShieldCheck,
  Users,
} from "lucide-react";

import { auth } from "@/auth";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getMessages } from "@/lib/i18n";
import {
  hasAnyPermission,
  hasEveryPermission,
  hasPermission,
} from "@/lib/permissions";
import type { CandidateSummary, Position } from "@/lib/types";
import type { AiOrganizationSettings } from "@/lib/ai/types";
import type { ApiResponseSchema } from "@/lib/generated/contracts";
import { SidebarShell } from "@/components/app/sidebar-shell";
import { SignOutLink } from "@/components/app/sign-out-link";
import { ThemeToggle } from "@/components/app/theme-toggle";
import { ProductSwitcher } from "@/components/ai/product-switcher";
import type { NavItem } from "@/components/app/sidebar-nav";

function initials(email: string): string {
  const local = email.split("@")[0] ?? "";
  const parts = local.replace(/[._-]/g, " ").split(" ").filter(Boolean);
  const first = parts[0]?.[0] ?? email[0] ?? "?";
  const second = parts[1]?.[0] ?? "";
  return (first + second).toUpperCase();
}

export async function Sidebar() {
  const [session, m, permissions] = await Promise.all([
    auth(),
    getMessages(),
    getCurrentPermissionMap(),
  ]);
  const email = session?.user?.email ?? "guest";
  const role = session?.user?.role ?? "user";

  const canReadCandidates = hasPermission(permissions, "candidates.read");
  const canUseAi = hasEveryPermission(permissions, [
    "ai.use",
    "ai.sensitive_context",
  ]);
  const canManageAi = hasPermission(permissions, "ai.manage");
  const canReadConversations =
    hasPermission(permissions, "communications.read") || canUseAi;
  const canAccessSourcing =
    hasAnyPermission(permissions, ["integrations.read", "integrations.manage"])
    || (canReadCandidates
      && hasAnyPermission(permissions, [
        "sourcing.read",
        "sourcing.manage",
      ]))
    || hasPermission(permissions, "analytics.read");
  const canAccessIntegrations = hasPermission(permissions, "integrations.read");
  const canAccessAssessments =
    hasEveryPermission(permissions, [
      "applications.read",
      "candidates.read",
      "assessments.read",
    ]) &&
    (hasPermission(permissions, "assessments.manage") ||
      hasEveryPermission(permissions, [
        "assessments.grade",
        "assessments.results",
      ]));
  const canAccessEnterprise =
    hasAnyPermission(permissions, [
      "enterprise.read",
      "enterprise.manage",
      "enterprise.audit_export",
      "enterprise.backup_export",
      "integrations.read",
    ]) ||
    hasEveryPermission(permissions, [
      "integrations.manage",
      "integrations.credentials",
    ]) ||
    hasEveryPermission(permissions, ["ai.manage", "ai.managed_provider"]);

  let positionCount: number | null = null;
  let candidateCount: number | null = null;
  let chatCount: number | null = null;
  let aiEnabled = false;
  const aiCapabilityRequest = canManageAi
    ? apiGet<AiOrganizationSettings>("/ai/v1/settings")
    : canUseAi
      ? apiGet<ApiResponseSchema<"HiringMissionPage">>("/ai/v1/missions?limit=1")
      : Promise.resolve(null);
  const [posRes, candRes, chatRes, aiRes] = await Promise.allSettled([
    canReadCandidates ? apiGetAll<Position>("/positions") : Promise.resolve(null),
    canReadCandidates ? apiGetAll<CandidateSummary>("/candidates") : Promise.resolve(null),
    canUseAi
      ? apiGetAll<ApiResponseSchema<"ChatSessionListItem">>("/chat/sessions")
      : Promise.resolve(null),
    aiCapabilityRequest,
  ]);
  if (posRes.status === "fulfilled" && posRes.value) positionCount = posRes.value.length;
  if (candRes.status === "fulfilled" && candRes.value) candidateCount = candRes.value.length;
  if (chatRes.status === "fulfilled" && chatRes.value) chatCount = chatRes.value.length;
  if (aiRes.status === "fulfilled" && aiRes.value) {
    aiEnabled = canManageAi
      ? (aiRes.value as AiOrganizationSettings).feature_enabled
      : true;
  }

  const workspace: NavItem[] = [
    ...(hasPermission(permissions, "account.read")
      ? [
          {
            label: m.app.nav.myWork,
            href: "/my-work",
            icon: <ListTodo className="h-3.5 w-3.5" />,
            match: "/my-work",
          },
        ]
      : []),
    ...(canReadCandidates
      ? [
          {
            label: m.app.nav.positions,
            href: "/dashboard",
            icon: <LayoutGrid className="h-3.5 w-3.5" />,
            count: positionCount,
            match: ["/dashboard", "/positions"],
          },
        ]
      : []),
    ...(hasPermission(permissions, "requisitions.read")
      ? [
          {
            label: m.app.nav.requisitions,
            href: "/requisitions",
            icon: <BriefcaseBusiness className="h-3.5 w-3.5" />,
            match: "/requisitions",
          },
        ]
      : []),
    ...(canReadCandidates
      ? [
          {
            label: m.app.nav.candidates,
            href: "/candidates",
            icon: <Users className="h-3.5 w-3.5" />,
            count: candidateCount,
            match: "/candidates",
          },
        ]
      : []),
    ...(hasPermission(permissions, "talent.read")
      ? [
          {
            label: m.app.nav.talent,
            href: "/talent",
            icon: <FolderHeart className="h-3.5 w-3.5" />,
            match: "/talent",
          },
        ]
      : []),
    ...(canReadConversations
      ? [
          {
            label: m.app.nav.conversations,
            href: "/conversations",
            icon: <MessageSquare className="h-3.5 w-3.5" />,
            count: chatCount,
            match: "/conversations",
          },
        ]
      : []),
    ...(hasPermission(permissions, "analytics.read")
      ? [
          {
            label: m.app.nav.analytics,
            href: "/analytics",
            icon: <BarChart3 className="h-3.5 w-3.5" />,
            match: "/analytics",
          },
        ]
      : []),
    ...(canAccessAssessments
      ? [
          {
            label: m.app.nav.assessments,
            href: "/assessments",
            icon: <ClipboardCheck className="h-3.5 w-3.5" />,
            match: "/assessments",
          },
        ]
      : []),
  ];

  const settings: NavItem[] = [
    {
      label: m.settings.title,
      href: "/settings",
      icon: <Settings className="h-3.5 w-3.5" />,
      match: "/settings",
    },
    ...(canAccessIntegrations
      ? [
          {
            label: m.app.nav.integrations,
            href: "/integrations",
            icon: <PlugZap className="h-3.5 w-3.5" />,
            match: "/integrations",
          },
        ]
      : []),
    ...(canAccessSourcing
      ? [
          {
            label: m.app.nav.sourcing,
            href: "/sourcing",
            icon: <RadioTower className="h-3.5 w-3.5" />,
            match: "/sourcing",
          },
        ]
      : []),
    ...(canAccessEnterprise
      ? [
          {
            label: m.app.nav.enterprise,
            href: "/enterprise",
            icon: <ShieldCheck className="h-3.5 w-3.5" />,
            match: "/enterprise",
          },
        ]
      : []),
  ];

  return (
    <SidebarShell
      brand={{ name: m.app.brandName, version: m.app.brandTag }}
      workspaceLabel={m.app.workspace}
      workspace={workspace}
      settingsLabel={m.app.settings}
      settings={settings}
      signOut={<SignOutLink />}
      themeLabel={m.app.theme}
      themeToggle={<ThemeToggle />}
      user={{ email, role, initials: initials(email) }}
      pinLabel={m.app.pinSidebar}
      unpinLabel={m.app.unpinSidebar}
      menuLabel={m.app.navigationMenu}
      closeMenuLabel={m.app.closeNavigation}
      productSwitcher={aiEnabled && canUseAi ? <ProductSwitcher current="human" /> : undefined}
    />
  );
}
