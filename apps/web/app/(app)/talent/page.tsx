import { auth } from "@/auth";
import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { TalentCrm } from "@/components/talent/talent-crm";
import type { TalentCrmProps } from "@/components/talent/talent-crm";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { getMessages } from "@/lib/i18n";

export const dynamic = "force-dynamic";

/** @ui-only Server-render load state with a recoverable partial-failure flag. */
type Loaded<T> = { data: T; failed: boolean };

async function safe<T>(path: string, fallback: T): Promise<Loaded<T>> {
  try {
    return { data: await apiGet<T>(path), failed: false };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return { data: fallback, failed: true };
  }
}

async function safeCollection<T>(path: string): Promise<Loaded<T[]>> {
  try {
    return { data: await apiGetAll<T>(path), failed: false };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return { data: [], failed: true };
  }
}

export default async function TalentPage() {
  const [m, session] = await Promise.all([getMessages(), auth()]);
  const [pools, sequences, templates, positions] = await Promise.all([
    safeCollection<TalentCrmProps["initialPools"][number]>("/talent/pools"),
    safeCollection<TalentCrmProps["initialSequences"][number]>(
      "/talent/nurture-sequences",
    ),
    safeCollection<TalentCrmProps["templates"][number]>(
      "/communications/templates?include_inactive=false",
    ),
    safeCollection<TalentCrmProps["positions"][number]>("/positions"),
  ]);
  return (
    <>
      <Topbar
        crumbs={[
          { label: m.app.nav.candidates, href: "/candidates" },
          {
            label:
              session?.user?.locale === "en" ? "Talent CRM" : "Кадровый резерв",
          },
        ]}
        actions={<LocaleSwitcher />}
      />
      <div className="flex-1 overflow-y-auto px-7 py-8">
        <TalentCrm
          initialPools={pools.data}
          initialSequences={sequences.data}
          templates={templates.data}
          positions={positions.data}
          loadFailures={{
            pools: pools.failed,
            sequences: sequences.failed,
            templates: templates.failed,
            positions: positions.failed,
          }}
        />
      </div>
    </>
  );
}
