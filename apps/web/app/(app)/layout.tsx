import { Sidebar } from "@/components/app/sidebar";
import { CommandPalette } from "@/components/app/command-palette";
import { getCurrentPermissionMap } from "@/lib/current-permissions";

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const permissions = await getCurrentPermissionMap();
  return (
    <div className="flex h-screen w-full overflow-hidden">
      <Sidebar />
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">{children}</main>
      <CommandPalette permissions={permissions} />
    </div>
  );
}
