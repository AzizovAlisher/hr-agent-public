"use client";

import { CalendarClock, MapPin, Video } from "lucide-react";
import Link from "next/link";

import { useI18n } from "@/components/i18n/locale-provider";
import type { InterviewAppointment } from "@/lib/types";

export function TodaysAppointments({ items }: { items: InterviewAppointment[] }) {
  const { locale } = useI18n();
  const ru = locale === "ru";
  if (items.length === 0) return null;
  const dateLocale = ru ? "ru-RU" : "en-US";
  return (
    <section className="mb-6 rounded-xl border border-border bg-bg-elevated">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <CalendarClock className="h-4 w-4 text-accent" />
        <h2 className="text-sm font-semibold">
          {ru ? "Интервью сегодня" : "Interviews today"}
        </h2>
        <span className="rounded bg-bg px-1.5 py-0.5 font-mono text-[10px] text-text-muted">
          {items.length}
        </span>
      </div>
      <div className="grid divide-y divide-border lg:grid-cols-2 lg:divide-x lg:divide-y-0">
        {items.map((item) => (
          <Link
            key={item.id}
            href={`/candidates/${item.candidate_id}${item.position_id ? `?position=${item.position_id}` : ""}`}
            className="flex items-center gap-3 px-4 py-3 hover:bg-bg-hover"
          >
            <span className="w-12 font-mono text-sm font-semibold">
              {new Intl.DateTimeFormat(dateLocale, {
                hour: "2-digit",
                minute: "2-digit",
              }).format(new Date(item.starts_at))}
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{item.position_title}</p>
              <p className="truncate text-xs text-text-muted">
                {item.interviewer_labels.join(", ") || (ru ? "Команда найма" : "Hiring team")}
              </p>
            </div>
            {item.location_mode === "video" ? (
              <Video className="h-4 w-4 text-text-muted" />
            ) : (
              <MapPin className="h-4 w-4 text-text-muted" />
            )}
          </Link>
        ))}
      </div>
    </section>
  );
}
