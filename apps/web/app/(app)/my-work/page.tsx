import { ClipboardList } from "lucide-react";

import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { MyWorkList } from "@/components/work/my-work-list";
import { TodaysAppointments } from "@/components/work/todays-appointments";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { getMessages } from "@/lib/i18n";
import type { InterviewAppointment, MyWorkPage } from "@/lib/types";


export const dynamic = "force-dynamic";


const EMPTY_PAGE: MyWorkPage = {
  items: [],
  counts: {
    all: 0,
    overdue: 0,
    today: 0,
    upcoming: 0,
    undated: 0,
    interviews_today: 0,
    stale: 0,
    missing_feedback: 0,
  },
  today: "",
  next_cursor: null,
};


export default async function MyWorkPageRoute() {
  const m = await getMessages();
  let page = EMPTY_PAGE;
  let appointments: InterviewAppointment[] = [];
  let error: string | null = null;
  const [workResult, appointmentResult] = await Promise.allSettled([
    apiGet<MyWorkPage>("/my-work?limit=50&stale_days=7"),
    apiGetAll<InterviewAppointment>("/calendar/appointments/me"),
  ]);
  try {
    if (workResult.status === "rejected") throw workResult.reason;
    page = workResult.value;
    if (appointmentResult.status === "fulfilled") appointments = appointmentResult.value;
    else if ((appointmentResult.reason as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) {
      throw appointmentResult.reason;
    }
  } catch (cause) {
    if ((cause as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) {
      throw cause;
    }
    error = (cause as Error).message;
  }

  return (
    <>
      <Topbar
        crumbs={[{ label: m.myWork.crumb }]}
        actions={<LocaleSwitcher />}
      />
      <div className="flex-1 overflow-y-auto overflow-x-hidden px-7 py-8">
        <header className="mb-7 flex items-start gap-3">
          <span className="mt-0.5 flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-bg-elevated text-text-muted">
            <ClipboardList className="h-4 w-4" />
          </span>
          <div>
            <h1 className="text-[24px] font-semibold tracking-[-0.025em]">
              {m.myWork.title}
            </h1>
            <p className="mt-1 text-[13px] text-text-muted">
              {error ? m.myWork.subtitle : m.myWork.summary(page.counts.all)}
            </p>
          </div>
        </header>

        {error ? (
          <div className="rounded-lg border border-border bg-bg-elevated p-5 text-[13px] text-score-low">
            {error}
          </div>
        ) : (
          <>
            <TodaysAppointments items={appointments} />
            <MyWorkList initialPage={page} />
          </>
        )}
      </div>
    </>
  );
}
