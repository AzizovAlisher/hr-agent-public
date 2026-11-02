"use client";

import Link from "next/link";
import {
  ArrowRight,
  CalendarCheck,
  CalendarDays,
  Check,
  Clock3,
  Loader2,
  MapPin,
  RotateCcw,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, useTransition } from "react";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  bookPublicSchedulingSlot,
  getPublicSchedulingSlots,
} from "@/lib/public-scheduling";
import {
  browserTimeZone,
  SCHEDULING_TIMEZONES,
  schedulingDate,
  schedulingDateTime,
  schedulingDayKey,
  schedulingTime,
} from "@/lib/scheduling-time";
import type {
  PublicAppointment,
  PublicSchedulingDetails,
  SchedulingSlot,
  SchedulingSlotPage,
} from "@/lib/types";
import { cn } from "@/lib/utils";


/** @ui-only Booking response transformed to a same-origin management path. */
type BookingConfirmation = {
  appointment: PublicAppointment;
  managePath: string;
};


function freshIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `booking-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}


export function PublicSchedulingPicker({
  token,
  details,
  initialSlots,
  initialErrorCode,
}: {
  token: string;
  details: PublicSchedulingDetails;
  initialSlots: SchedulingSlotPage | null;
  initialErrorCode: string | null;
}) {
  const { locale, m } = useI18n();
  const copy = m.scheduling.public;
  const common = m.scheduling.common;
  const initialTimezone = initialSlots?.timezone ?? "UTC";
  const [timezone, setTimezone] = useState(initialTimezone);
  const [slots, setSlots] = useState(initialSlots?.items ?? []);
  const [slotsError, setSlotsError] = useState(initialErrorCode);
  const [selectedStart, setSelectedStart] = useState<string | null>(null);
  const [bookingError, setBookingError] = useState<string | null>(null);
  const [confirmation, setConfirmation] =
    useState<BookingConfirmation | null>(null);
  const [slotsPending, startSlotsTransition] = useTransition();
  const [bookingPending, startBookingTransition] = useTransition();
  const requestGeneration = useRef(0);
  const idempotencyKey = useRef<string | null>(null);
  const detectedOnce = useRef(false);
  const [detected, setDetected] = useState(initialTimezone);
  const timezoneOptions = useMemo(
    () => Array.from(new Set([detected, initialTimezone, ...SCHEDULING_TIMEZONES])),
    [detected, initialTimezone],
  );

  function loadSlots(nextTimezone: string) {
    const generation = ++requestGeneration.current;
    setTimezone(nextTimezone);
    setSelectedStart(null);
    setBookingError(null);
    idempotencyKey.current = null;
    startSlotsTransition(async () => {
      const result = await getPublicSchedulingSlots(token, {
        timezone: nextTimezone,
        days: 14,
      });
      if (generation !== requestGeneration.current) return;
      if (!result.ok) {
        setSlots([]);
        setSlotsError(result.code);
        return;
      }
      setSlots(result.data.items);
      setSlotsError(null);
    });
  }

  useEffect(() => {
    if (detectedOnce.current) return;
    detectedOnce.current = true;
    const browserZone = browserTimeZone(initialTimezone);
    setDetected(browserZone);
    if (browserZone !== initialTimezone) loadSlots(browserZone);
    // The initial values are immutable for this token; run browser detection once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const selectedSlot = slots.find((slot) => slot.starts_at === selectedStart) ?? null;

  function selectSlot(slot: SchedulingSlot) {
    setSelectedStart(slot.starts_at);
    setBookingError(null);
    idempotencyKey.current = null;
  }

  function book() {
    if (!selectedSlot || bookingPending) return;
    idempotencyKey.current ??= freshIdempotencyKey();
    startBookingTransition(async () => {
      const result = await bookPublicSchedulingSlot(token, {
        starts_at: selectedSlot.starts_at,
        timezone,
        idempotency_key: idempotencyKey.current!,
      });
      if (!result.ok) {
        setBookingError(result.code);
        if (["slot_unavailable", "booking_conflict"].includes(result.code)) {
          idempotencyKey.current = null;
          setSelectedStart(null);
          loadSlots(timezone);
        }
        return;
      }
      setBookingError(null);
      setConfirmation(result.data);
    });
  }

  if (confirmation) {
    return (
      <section className="rounded-xl border border-accent-border bg-bg-elevated p-6 text-center sm:p-8">
        <span className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-primary text-primary-foreground">
          <Check className="h-6 w-6" />
        </span>
        <h1 className="mt-4 text-[20px] font-semibold tracking-tight">
          {copy.bookedTitle}
        </h1>
        <p className="mx-auto mt-2 max-w-lg text-[12.5px] leading-5 text-text-muted">
          {copy.bookedDesc}
        </p>
        <div className="mx-auto mt-5 max-w-md rounded-lg border border-border bg-bg px-4 py-3 text-left">
          <p className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {copy.selectedSlot}
          </p>
          <p className="mt-1 text-[13px] font-medium text-text">
            {schedulingDateTime(
              confirmation.appointment.starts_at,
              locale,
              confirmation.appointment.candidate_timezone,
            )}
          </p>
        </div>
        <Link
          href={confirmation.managePath}
          referrerPolicy="no-referrer"
          className={cn(buttonVariants(), "mt-5")}
        >
          {copy.manage}
          <ArrowRight className="h-4 w-4" />
        </Link>
      </section>
    );
  }

  return (
    <>
      <header className="rounded-xl border border-border bg-bg-elevated p-6">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
          {copy.title}
        </span>
        <h1 className="mt-2 text-[23px] font-semibold leading-tight tracking-[-0.02em]">
          {details.position_title}
        </h1>
        <p className="mt-2 text-[13px] leading-5 text-text-muted">{copy.subtitle}</p>
        <div className="mt-4 flex flex-wrap gap-2 text-[11px] text-text-muted">
          <Meta icon={<Clock3 className="h-3.5 w-3.5" />}>
            {copy.duration}: {common.minutes(details.duration_minutes)}
          </Meta>
          <Meta icon={<MapPin className="h-3.5 w-3.5" />}>
            {copy.location}: {common.locationModes[details.location_mode]}
          </Meta>
          <Meta icon={<CalendarDays className="h-3.5 w-3.5" />}>
            {copy.expires(schedulingDateTime(details.expires_at, locale))}
          </Meta>
        </div>
      </header>

      <section className="rounded-xl border border-border bg-bg-elevated p-5 sm:p-6">
        <label className="block">
          <span className="text-[11px] font-medium text-text-muted">
            {copy.timezoneLabel}
          </span>
          <select
            value={timezone}
            onChange={(event) => loadSlots(event.target.value)}
            disabled={slotsPending || bookingPending}
            className="mt-1.5 block w-full rounded-[6px] border border-border bg-bg px-3 py-2.5 font-mono text-[12px] text-text outline-none transition-colors focus:border-border-strong disabled:opacity-60"
          >
            {timezoneOptions.map((zone) => (
              <option key={zone} value={zone}>
                {zone}
              </option>
            ))}
          </select>
          <span className="mt-1.5 block text-[10.5px] text-text-faint">
            {copy.timezoneHint}
          </span>
        </label>

        <div className="mt-6">
          <h2 className="text-[13px] font-semibold text-text">{copy.slotsTitle}</h2>
          {slotsPending ? (
            <div className="flex items-center justify-center gap-2 py-14 text-[12px] text-text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              {copy.slotsLoading}
            </div>
          ) : slotsError ? (
            <div className="flex flex-col items-center gap-3 py-12 text-center">
              <p className="text-[12px] text-text-muted">
                {copy.errors[slotsError] ?? copy.slotsFailed}
              </p>
              <Button size="sm" variant="outline" onClick={() => loadSlots(timezone)}>
                <RotateCcw className="h-3.5 w-3.5" />
                {common.retry}
              </Button>
            </div>
          ) : groups.length === 0 ? (
            <p className="py-12 text-center text-[12px] leading-5 text-text-muted">
              {copy.slotsEmpty}
            </p>
          ) : (
            <div className="mt-3 flex flex-col gap-5">
              {groups.map((group) => (
                <div key={group.key}>
                  <h3 className="mb-2 text-[11px] font-medium capitalize text-text-muted">
                    {group.label}
                  </h3>
                  <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5">
                    {group.items.map((slot) => {
                      const selected = slot.starts_at === selectedStart;
                      return (
                        <button
                          key={slot.starts_at}
                          type="button"
                          aria-pressed={selected}
                          onClick={() => selectSlot(slot)}
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
        </div>

        <div className="mt-6 border-t border-border pt-4">
          {selectedSlot ? (
            <div className="mb-3 rounded-lg border border-accent-border bg-accent-muted px-3.5 py-3">
              <p className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {copy.selectedSlot}
              </p>
              <p className="mt-1 text-[12.5px] font-medium text-text">
                {schedulingDateTime(selectedSlot.starts_at, locale, timezone)}
              </p>
            </div>
          ) : (
            <p className="mb-3 text-[11.5px] text-text-muted">{copy.selectSlot}</p>
          )}
          {bookingError ? (
            <p role="alert" className="mb-3 rounded-[6px] border border-destructive/30 bg-destructive/10 px-3 py-2 text-[11.5px] text-destructive">
              {copy.errors[bookingError] ?? copy.errors.request_failed}
            </p>
          ) : null}
          <Button className="w-full" disabled={!selectedSlot || bookingPending || slotsPending} onClick={book}>
            {bookingPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CalendarCheck className="h-4 w-4" />}
            {bookingPending ? copy.booking : copy.book}
          </Button>
        </div>
      </section>
    </>
  );
}


function Meta({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-2.5 py-1.5">
      {icon}
      {children}
    </span>
  );
}
