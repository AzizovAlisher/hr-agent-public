"use client";

import {
  CalendarClock,
  CalendarDays,
  Clock3,
  Loader2,
  MapPin,
  RotateCcw,
  Users,
  XCircle,
} from "lucide-react";
import { useMemo, useRef, useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  cancelPublicAppointment,
  getPublicRescheduleSlots,
  reschedulePublicAppointment,
} from "@/lib/public-scheduling";
import {
  SCHEDULING_TIMEZONES,
  schedulingDate,
  schedulingDateTime,
  schedulingDayKey,
  schedulingTime,
} from "@/lib/scheduling-time";
import type {
  PublicAppointmentDetails,
  SchedulingSlot,
} from "@/lib/types";
import { cn } from "@/lib/utils";


export function PublicAppointmentManager({
  manageToken,
  initialDetails,
}: {
  manageToken: string;
  initialDetails: PublicAppointmentDetails;
}) {
  const { locale, m } = useI18n();
  const copy = m.scheduling.public;
  const common = m.scheduling.common;
  const [details, setDetails] = useState(initialDetails);
  const [showReschedule, setShowReschedule] = useState(false);
  const [timezone, setTimezone] = useState(
    initialDetails.appointment.candidate_timezone,
  );
  const [slots, setSlots] = useState<SchedulingSlot[]>([]);
  const [selectedStart, setSelectedStart] = useState<string | null>(null);
  const [slotsError, setSlotsError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [slotsPending, startSlotsTransition] = useTransition();
  const [actionPending, startActionTransition] = useTransition();
  const generation = useRef(0);
  const appointment = details.appointment;
  const timezoneOptions = useMemo(
    () =>
      Array.from(
        new Set([
          appointment.candidate_timezone,
          ...SCHEDULING_TIMEZONES,
        ]),
      ),
    [appointment.candidate_timezone],
  );

  function loadSlots(nextTimezone: string) {
    const request = ++generation.current;
    setTimezone(nextTimezone);
    setSelectedStart(null);
    setActionError(null);
    startSlotsTransition(async () => {
      const result = await getPublicRescheduleSlots(manageToken, {
        timezone: nextTimezone,
        days: 14,
      });
      if (request !== generation.current) return;
      if (!result.ok) {
        setSlots([]);
        setSlotsError(result.code);
        return;
      }
      setSlots(result.data.items);
      setSlotsError(null);
    });
  }

  function openReschedule() {
    setShowReschedule(true);
    if (slots.length === 0) loadSlots(timezone);
  }

  const groups = useMemo(() => {
    const grouped = new Map<string, SchedulingSlot[]>();
    for (const slot of slots) {
      const key = schedulingDayKey(slot.starts_at, timezone);
      const current = grouped.get(key) ?? [];
      current.push(slot);
      grouped.set(key, current);
    }
    return [...grouped.entries()].map(([key, items]) => ({
      key,
      label: schedulingDate(items[0].starts_at, locale, timezone),
      items,
    }));
  }, [locale, slots, timezone]);

  function reschedule() {
    if (!selectedStart || actionPending) return;
    startActionTransition(async () => {
      const result = await reschedulePublicAppointment(manageToken, {
        starts_at: selectedStart,
        timezone,
        expected_version: appointment.version,
      });
      if (!result.ok) {
        setActionError(result.code);
        if (["slot_unavailable", "reschedule_conflict"].includes(result.code)) {
          setSelectedStart(null);
          loadSlots(timezone);
        }
        return;
      }
      setDetails(result.data);
      setShowReschedule(false);
      setSelectedStart(null);
      setSlots([]);
      setActionError(null);
      toast.success(copy.rescheduled);
    });
  }

  function cancel() {
    if (actionPending || !window.confirm(copy.cancelConfirm)) return;
    startActionTransition(async () => {
      const result = await cancelPublicAppointment(
        manageToken,
        appointment.version,
      );
      if (!result.ok) {
        setActionError(result.code);
        return;
      }
      setDetails(result.data);
      setShowReschedule(false);
      setActionError(null);
      toast.success(copy.cancelled);
    });
  }

  return (
    <>
      <header className="rounded-xl border border-border bg-bg-elevated p-6">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
          {copy.manageTitle}
        </span>
        <h1 className="mt-2 text-[23px] font-semibold leading-tight tracking-[-0.02em]">
          {appointment.position_title}
        </h1>
        <p className="mt-2 text-[13px] leading-5 text-text-muted">
          {copy.manageSubtitle}
        </p>
      </header>

      <section className="rounded-xl border border-border bg-bg-elevated p-5 sm:p-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {copy.appointment}
            </p>
            <p className="mt-1.5 text-[16px] font-semibold text-text">
              {schedulingDateTime(
                appointment.starts_at,
                locale,
                appointment.candidate_timezone,
              )}
            </p>
          </div>
          <span
            className={cn(
              "w-fit rounded-[5px] px-2.5 py-1.5 text-[11px] font-medium",
              appointment.status === "scheduled"
                ? "bg-accent-muted text-primary"
                : "bg-bg-active text-text-muted",
            )}
          >
            {common.appointmentStatuses[appointment.status]}
          </span>
        </div>

        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <Detail icon={<Clock3 className="h-4 w-4" />} label={copy.duration}>
            {common.minutes(
              Math.max(
                0,
                Math.round(
                  (new Date(appointment.ends_at).getTime() -
                    new Date(appointment.starts_at).getTime()) /
                    60_000,
                ),
              ),
            )}
          </Detail>
          <Detail icon={<MapPin className="h-4 w-4" />} label={copy.location}>
            {common.locationModes[appointment.location_mode]}
          </Detail>
          <Detail icon={<Users className="h-4 w-4" />} label={copy.interviewers}>
            {appointment.interviewer_labels.join(", ") || "—"}
          </Detail>
          <Detail icon={<CalendarDays className="h-4 w-4" />} label={common.timezone}>
            {appointment.candidate_timezone}
          </Detail>
        </div>

        {appointment.location_details ? (
          <p className="mt-4 break-words rounded-[7px] border border-border bg-bg px-3 py-2.5 text-[12px] text-text-muted">
            {appointment.location_details}
          </p>
        ) : null}

        <p className="mt-4 text-[10.5px] text-text-faint">
          {m.scheduling.candidate.providerSync(
            m.scheduling.settings.providerNames[appointment.provider],
            common.syncStatuses[appointment.sync_status],
          )}
        </p>

        {actionError ? (
          <div role="alert" className="mt-4 rounded-[7px] border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-[11.5px] text-destructive">
            <p>{copy.errors[actionError] ?? copy.errors.request_failed}</p>
            {actionError === "version_conflict" ? (
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="mt-2 inline-flex min-h-6 min-w-6 items-center px-1 font-medium underline underline-offset-2"
              >
                {copy.refresh}
              </button>
            ) : null}
          </div>
        ) : null}

        {appointment.status === "scheduled" ? (
          <div className="mt-5 flex flex-wrap gap-2 border-t border-border pt-4">
            <Button variant="outline" onClick={openReschedule} disabled={actionPending}>
              <CalendarClock className="h-4 w-4" />
              {copy.reschedule}
            </Button>
            <Button variant="destructive" onClick={cancel} disabled={actionPending}>
              {actionPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <XCircle className="h-4 w-4" />}
              {actionPending ? copy.cancelling : copy.cancelAppointment}
            </Button>
          </div>
        ) : null}
      </section>

      {showReschedule && appointment.status === "scheduled" ? (
        <section className="rounded-xl border border-border bg-bg-elevated p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[14px] font-semibold text-text">{copy.chooseNewTime}</h2>
              <p className="mt-1 text-[11px] text-text-faint">{copy.timezoneHint}</p>
            </div>
            <Button size="sm" variant="ghost" onClick={() => setShowReschedule(false)}>
              {m.common.cancel}
            </Button>
          </div>

          <select
            value={timezone}
            onChange={(event) => loadSlots(event.target.value)}
            disabled={slotsPending || actionPending}
            aria-label={copy.timezoneLabel}
            className="mt-4 block w-full rounded-[6px] border border-border bg-bg px-3 py-2.5 font-mono text-[12px] text-text outline-none focus:border-border-strong"
          >
            {timezoneOptions.map((zone) => (
              <option key={zone} value={zone}>{zone}</option>
            ))}
          </select>

          {slotsPending ? (
            <div className="flex items-center justify-center gap-2 py-12 text-[12px] text-text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              {copy.slotsLoading}
            </div>
          ) : slotsError ? (
            <div className="flex flex-col items-center gap-3 py-10 text-center">
              <p className="text-[12px] text-text-muted">
                {copy.errors[slotsError] ?? copy.slotsFailed}
              </p>
              <Button size="sm" variant="outline" onClick={() => loadSlots(timezone)}>
                <RotateCcw className="h-3.5 w-3.5" />
                {common.retry}
              </Button>
            </div>
          ) : groups.length === 0 ? (
            <p className="py-10 text-center text-[12px] text-text-muted">{copy.slotsEmpty}</p>
          ) : (
            <div className="mt-5 flex flex-col gap-5">
              {groups.map((group) => (
                <div key={group.key}>
                  <h3 className="mb-2 text-[11px] font-medium capitalize text-text-muted">
                    {group.label}
                  </h3>
                  <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5">
                    {group.items.map((slot) => {
                      const selected = selectedStart === slot.starts_at;
                      return (
                        <button
                          key={slot.starts_at}
                          type="button"
                          aria-pressed={selected}
                          onClick={() => {
                            setSelectedStart(slot.starts_at);
                            setActionError(null);
                          }}
                          className={cn(
                            "rounded-[7px] border px-2 py-2.5 font-mono text-[12px] font-medium transition-colors",
                            selected
                              ? "border-primary bg-primary text-primary-foreground"
                              : "border-border bg-bg text-text hover:border-accent-border hover:bg-accent-muted",
                          )}
                        >
                          {schedulingTime(slot.starts_at, locale, timezone)}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}

          {selectedStart ? (
            <p className="mt-5 rounded-[7px] border border-accent-border bg-accent-muted px-3 py-2.5 text-[12px] font-medium">
              {schedulingDateTime(selectedStart, locale, timezone)}
            </p>
          ) : null}
          <Button className="mt-4 w-full" onClick={reschedule} disabled={!selectedStart || slotsPending || actionPending}>
            {actionPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CalendarClock className="h-4 w-4" />}
            {actionPending ? copy.rescheduling : copy.confirmReschedule}
          </Button>
        </section>
      ) : null}
    </>
  );
}


function Detail({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex gap-2.5 rounded-[8px] border border-border bg-bg px-3 py-3">
      <span className="mt-0.5 text-text-faint">{icon}</span>
      <div className="min-w-0">
        <p className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {label}
        </p>
        <p className="mt-1 break-words text-[11.5px] text-text-muted">{children}</p>
      </div>
    </div>
  );
}
