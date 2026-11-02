import { Topbar } from "@/components/app/topbar";
import { notFound } from "next/navigation";
import { NewProjectFlow } from "@/components/positions/new-project-flow";
import { getMessages } from "@/lib/i18n";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getCandidateWorkspaceAccess } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function NewProjectPage() {
  const [m, permissionMap] = await Promise.all([
    getMessages(),
    getCurrentPermissionMap(),
  ]);
  if (!getCandidateWorkspaceAccess(permissionMap).createPositions) notFound();
  return (
    <>
      <Topbar
        crumbs={[
          { label: m.position.crumb, href: "/dashboard" },
          { label: m.newProject.crumb },
        ]}
      />
      <div className="flex h-[calc(100vh-53px)] flex-col">
        <NewProjectFlow />
      </div>
    </>
  );
}
