import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  Clock3,
  LoaderCircle,
  PauseCircle,
  ShieldAlert,
  XCircle,
} from "lucide-react";

import type { Locale } from "@/lib/i18n/types";
import type {
  AiActionBundleStatus,
  HiringMissionStatus,
  MissionStepStatus,
} from "@/lib/ai/types";
import { cn } from "@/lib/utils";

const missionLabels: Record<Locale, Record<HiringMissionStatus, string>> = {
  en: {
    draft: "Draft",
    planning: "Planning",
    running: "Running",
    awaiting_approval: "Needs approval",
    completed: "Completed",
    stopped: "Stopped",
    failed: "Needs attention",
  },
  ru: {
    draft: "Черновик",
    planning: "Планирование",
    running: "В работе",
    awaiting_approval: "Нужно согласование",
    completed: "Завершена",
    stopped: "Остановлена",
    failed: "Нужна помощь",
  },
};

const bundleLabels: Record<Locale, Record<AiActionBundleStatus, string>> = {
  en: {
    pending: "Pending",
    approved: "Approved",
    rejected: "Rejected",
    executed: "Executed",
    failed: "Failed",
    stale: "Outdated",
  },
  ru: {
    pending: "Ожидает",
    approved: "Согласовано",
    rejected: "Отклонено",
    executed: "Выполнено",
    failed: "Ошибка",
    stale: "Устарело",
  },
};

const stepLabels: Record<Locale, Record<MissionStepStatus, string>> = {
  en: {
    pending: "Pending",
    ready: "Ready",
    running: "Running",
    completed: "Completed",
    failed: "Failed",
    blocked: "Blocked",
    cancelled: "Cancelled",
    skipped: "Skipped",
  },
  ru: {
    pending: "Ожидает",
    ready: "Готов",
    running: "В работе",
    completed: "Завершён",
    failed: "Ошибка",
    blocked: "Заблокирован",
    cancelled: "Отменён",
    skipped: "Пропущен",
  },
};

function statusTone(status: string) {
  if (["completed", "approved", "executed"].includes(status)) {
    return "border-score-high/25 bg-score-high/10 text-score-high";
  }
  if (["failed", "rejected", "blocked"].includes(status)) {
    return "border-score-low/25 bg-score-low/10 text-score-low";
  }
  if (["awaiting_approval", "pending", "ready"].includes(status)) {
    return "border-score-mid/25 bg-score-mid/10 text-score-mid";
  }
  if (["running", "planning"].includes(status)) {
    return "border-accent-border bg-accent-muted text-primary";
  }
  return "border-border bg-bg-hover text-text-muted";
}

function StatusIcon({ status }: { status: string }) {
  const cls = "h-3 w-3";
  if (["completed", "approved", "executed"].includes(status)) return <CheckCircle2 className={cls} />;
  if (["failed", "rejected"].includes(status)) return <XCircle className={cls} />;
  if (status === "blocked") return <AlertCircle className={cls} />;
  if (status === "awaiting_approval") return <ShieldAlert className={cls} />;
  if (["running", "planning"].includes(status)) return <LoaderCircle className={cn(cls, "animate-spin")} />;
  if (["stopped", "cancelled"].includes(status)) return <PauseCircle className={cls} />;
  if (["pending", "ready"].includes(status)) return <Clock3 className={cls} />;
  return <CircleDashed className={cls} />;
}

export function MissionStatusBadge({
  status,
  locale,
}: {
  status: HiringMissionStatus;
  locale: Locale;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium", statusTone(status))}>
      <StatusIcon status={status} />
      {missionLabels[locale][status]}
    </span>
  );
}

export function BundleStatusBadge({
  status,
  locale,
}: {
  status: AiActionBundleStatus;
  locale: Locale;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium", statusTone(status))}>
      <StatusIcon status={status} />
      {bundleLabels[locale][status]}
    </span>
  );
}

export function StepStatus({ status, locale }: { status: MissionStepStatus; locale: Locale }) {
  return (
    <span className={cn("inline-flex items-center gap-1 text-[10px] font-medium", statusTone(status).split(" ").at(-1))}>
      <StatusIcon status={status} />
      {stepLabels[locale][status]}
    </span>
  );
}

