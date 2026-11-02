import { unstable_rethrow } from "next/navigation";

import { auth } from "@/auth";
import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { RequisitionsWorkspace } from "@/components/requisitions/requisitions-workspace";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { getMessages } from "@/lib/i18n";
import type {
  AssignableUser,
  EffectivePermissions,
  EnterpriseFieldVisibility,
  Requisition,
} from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function RequisitionsPage() {
  const [session, m] = await Promise.all([auth(), getMessages()]);
  const [requisitionsResult, usersResult, permissionsResult, policyResult] =
    await Promise.allSettled([
      apiGetAll<Requisition>("/requisitions"),
      apiGetAll<AssignableUser>("/users/assignable"),
      apiGet<EffectivePermissions>("/enterprise/permissions/me"),
      apiGet<EnterpriseFieldVisibility>("/enterprise/policy"),
    ]);

  for (const result of [
    requisitionsResult,
    usersResult,
    permissionsResult,
    policyResult,
  ]) {
    if (result.status === "rejected") unstable_rethrow(result.reason);
  }

  const role = session?.user?.role ?? "recruiter";
  const permissions =
    permissionsResult.status === "fulfilled"
      ? permissionsResult.value.permissions
      : {};
  const compensationRoles =
    policyResult.status === "fulfilled"
      ? policyResult.value.field_visibility.compensation ?? []
      : [];
  const canManage = permissions["requisitions.manage"] === true;
  const canApprove = permissions["requisitions.approve"] === true;
  const canViewCompensation =
    permissions["requisitions.compensation"] === true &&
    compensationRoles.includes(role);

  return (
    <>
      <Topbar
        crumbs={[{ label: m.requisitions.crumb }]}
        actions={<LocaleSwitcher />}
      />
      <div className="flex-1 overflow-y-auto overflow-x-hidden px-7 py-8">
        <RequisitionsWorkspace
          initialRequisitions={
            requisitionsResult.status === "fulfilled"
              ? requisitionsResult.value
              : []
          }
          users={usersResult.status === "fulfilled" ? usersResult.value : []}
          currentUserId={
            (session?.user as { id?: string } | undefined)?.id ?? ""
          }
          canManage={canManage}
          canApprove={canApprove}
          canViewCompensation={canViewCompensation}
          initialLoadFailed={requisitionsResult.status === "rejected"}
        />
      </div>
    </>
  );
}
