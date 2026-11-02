import { Topbar } from "@/components/app/topbar";
import { notFound } from "next/navigation";
import { NewPositionFlow } from "@/components/positions/new-position-flow";
import { auth } from "@/auth";
import { apiGetAll } from "@/lib/api-client";
import { getMessages } from "@/lib/i18n";
import type { Position } from "@/lib/types";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getCandidateWorkspaceAccess } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function NewPositionPage() {
  const [m, permissionMap, session] = await Promise.all([
    getMessages(),
    getCurrentPermissionMap(),
    auth(),
  ]);
  if (
    !session?.user?.id ||
    !session.user.organization?.id ||
    !getCandidateWorkspaceAccess(permissionMap).createPositions
  ) {
    notFound();
  }
  // Pull existing folder names so the flow can offer them as quick-pick
  // options. Failure is non-fatal — the input still allows free text.
  let availableFolders: string[] = [];
  try {
    const positions = await apiGetAll<Position>("/positions");
    availableFolders = Array.from(
      new Set(
        positions
          .map((p) => p.folder?.trim())
          .filter((f): f is string => !!f),
      ),
    ).sort((a, b) => a.localeCompare(b));
  } catch {
    /* ignore — folder picker still works as a free-text input */
  }
  return (
    <>
      <Topbar
        crumbs={[
          { label: m.position.crumb, href: "/dashboard" },
          { label: m.newPosition.crumb },
        ]}
      />
      <div className="grid h-[calc(100vh-53px)] grid-cols-1 grid-rows-[minmax(0,1fr)_minmax(0,1fr)] lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] lg:grid-rows-1">
        <NewPositionFlow
          availableFolders={availableFolders}
          draftScope={`${session.user.organization.id}:${session.user.id}`}
        />
      </div>
    </>
  );
}
