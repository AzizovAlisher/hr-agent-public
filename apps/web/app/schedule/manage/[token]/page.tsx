import { CalendarX2 } from "lucide-react";

import { PublicAppointmentManager } from "@/components/public/public-appointment-manager";
import { SchedulingFrame } from "@/components/public/scheduling-frame";
import { getMessages } from "@/lib/i18n";
import { getPublicAppointmentDetails } from "@/lib/public-scheduling";


export const dynamic = "force-dynamic";


export default async function PublicAppointmentPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const [{ token }, m] = await Promise.all([params, getMessages()]);
  const [detailsResult] = await Promise.allSettled([
    getPublicAppointmentDetails(token),
  ]);
  const details =
    detailsResult.status === "fulfilled" ? detailsResult.value : null;

  if (!details?.ok) {
    const code = details && !details.ok ? details.code : "network_error";
    const message =
      m.scheduling.public.errors[code] ??
      m.scheduling.public.errors.request_failed;
    return (
      <SchedulingFrame
        organizationName={m.app.brandName}
        logoUrl={null}
        accentColor={null}
      >
        <section className="rounded-xl border border-border bg-bg-elevated px-6 py-14 text-center">
          <CalendarX2 className="mx-auto h-9 w-9 text-text-faint" />
          <h1 className="mt-4 text-[18px] font-semibold">
            {message}
          </h1>
        </section>
      </SchedulingFrame>
    );
  }

  return (
    <SchedulingFrame
      organizationName={details.data.organization_name}
      logoUrl={details.data.organization_logo_url}
      accentColor={details.data.accent_color}
    >
      <PublicAppointmentManager
        manageToken={token}
        initialDetails={details.data}
      />
    </SchedulingFrame>
  );
}
