"use client";

import {
  CalendarCheck,
  CalendarOff,
  Check,
  Clock3,
  Loader2,
  Plus,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { useMemo, useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  createAvailabilityBlockAction,
  deleteAvailabilityBlockAction,
  getMyAvailabilityAction,
  listAvailabilityBlocksAction,
  listCalendarProvidersAction,
  updateMyAvailabilityAction,
} from "@/lib/actions";
import {
  SCHEDULING_TIMEZONES,
  schedulingDateTime,
} from "@/lib/scheduling-time";
import type {
  Availability,
  AvailabilityBlock,
  AvailabilityInterval,
  CalendarProvider,
} from "@/lib/types";
import { cn } from "@/lib/utils";


/** @ui-only Availability interval with a stable React editor key. */
type EditableInterval = AvailabilityInterval & { key: string };


function intervalKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}


function editableIntervals(items: AvailabilityInterval[]): EditableInterval[] {
  return items.map((item, index) => ({
    ...item,
    start_time: item.start_time.slice(0, 5),
    end_time: item.end_time.slice(0, 5),
    key: `saved-${item.weekday}-${item.start_time}-${item.end_time}-${index}`,
  }));
}


const FALLBACK_PROVIDERS: CalendarProvider[] = [
  {
    provider: "local",
    connection_id: null,
    name: "Local calendar",
    status: null,
    capability: "local",
    available: true,
    reason: null,
  },
];


function completeProviders(items: CalendarProvider[]): CalendarProvider[] {
  return items.length > 0 ? items : FALLBACK_PROVIDERS;
}


export function SchedulingSettingsTab({
  initialAvailability,
  initialBlocks,
  initialProviders,
  availabilityLoadFailed,
  blocksLoadFailed,
  providersLoadFailed,
  canManage,
}: {
  initialAvailability: Availability;
  initialBlocks: AvailabilityBlock[];
  initialProviders: CalendarProvider[];
  availabilityLoadFailed: boolean;
  blocksLoadFailed: boolean;
  providersLoadFailed: boolean;
  canManage: boolean;
}) {
  const { locale, m } = useI18n();
  const copy = m.scheduling.settings;
  const common = m.scheduling.common;
  const [timezone, setTimezone] = useState(initialAvailability.timezone);
  const [intervals, setIntervals] = useState<EditableInterval[]>(
    editableIntervals(initialAvailability.intervals),
  );
  const [blocks, setBlocks] = useState(initialBlocks);
  const [providers, setProviders] = useState(
    completeProviders(initialProviders),
  );
  const [availabilityFailed, setAvailabilityFailed] = useState(
    availabilityLoadFailed,
  );
  const [blocksFailed, setBlocksFailed] = useState(blocksLoadFailed);
  const [providersFailed, setProvidersFailed] = useState(providersLoadFailed);
  const [blockStart, setBlockStart] = useState("");
  const [blockEnd, setBlockEnd] = useState("");
  const [pending, startTransition] = useTransition();
  const timezoneOptions = useMemo(
    () => Array.from(new Set([timezone, ...SCHEDULING_TIMEZONES])),
    [timezone],
  );

  function providerStatus(provider: CalendarProvider): string {
    if (provider.available || provider.reason === "connected") return copy.active;
    if (provider.reason === "connecting") return copy.connecting;
    if (provider.reason === "degraded" || provider.reason === "error") return copy.degraded;
    if (provider.reason === "expired") return copy.expired;
    if (provider.reason === "revoked") return copy.revoked;
    if (provider.reason === "disconnected" || provider.reason === "disabled") {
      return copy.disconnected;
    }
    return copy.comingSoon;
  }

  function dayIntervals(weekday: number) {
    return intervals.filter((interval) => interval.weekday === weekday);
  }

  function enableDay(weekday: number, enabled: boolean) {
    setIntervals((current) => {
      const withoutDay = current.filter((interval) => interval.weekday !== weekday);
      if (!enabled) return withoutDay;
      return [
        ...withoutDay,
        {
          key: intervalKey(),
          weekday,
          start_time: "09:00",
          end_time: "17:00",
        },
      ].sort((a, b) => a.weekday - b.weekday || a.start_time.localeCompare(b.start_time));
    });
  }

  function addInterval(weekday: number) {
    setIntervals((current) => [
      ...current,
      {
        key: intervalKey(),
        weekday,
        start_time: "09:00",
        end_time: "17:00",
      },
    ]);
  }

  function updateInterval(
    key: string,
    field: "start_time" | "end_time",
    value: string,
  ) {
    setIntervals((current) =>
      current.map((interval) =>
        interval.key === key ? { ...interval, [field]: value } : interval,
      ),
    );
  }

  function removeInterval(key: string) {
    setIntervals((current) => current.filter((interval) => interval.key !== key));
  }

  function saveAvailability() {
    if (!canManage) return;
    const normalized = intervals
      .map(({ key: _key, ...interval }) => interval)
      .sort((a, b) => a.weekday - b.weekday || a.start_time.localeCompare(b.start_time));
    const unique = new Set(
      normalized.map(
        (interval) =>
          `${interval.weekday}:${interval.start_time}:${interval.end_time}`,
      ),
    );
    if (
      !timezone.trim() ||
      normalized.some(
        (interval) =>
          !interval.start_time ||
          !interval.end_time ||
          interval.start_time >= interval.end_time,
      ) ||
      unique.size !== normalized.length
    ) {
      toast.error(copy.invalidHours);
      return;
    }
    startTransition(async () => {
      const result = await updateMyAvailabilityAction({
        timezone: timezone.trim(),
        intervals: normalized,
      });
      if (!result.ok) {
        toast.error(copy.hoursSaveFailed);
        return;
      }
      setTimezone(result.data.timezone);
      setIntervals(editableIntervals(result.data.intervals));
      setAvailabilityFailed(false);
      toast.success(copy.hoursSaved);
    });
  }

  function addBlock(event: React.FormEvent) {
    event.preventDefault();
    if (!canManage) return;
    const start = new Date(blockStart);
    const end = new Date(blockEnd);
    if (
      !blockStart ||
      !blockEnd ||
      Number.isNaN(start.getTime()) ||
      Number.isNaN(end.getTime()) ||
      start >= end
    ) {
      toast.error(copy.blockFailed);
      return;
    }
    startTransition(async () => {
      const result = await createAvailabilityBlockAction({
        starts_at: start.toISOString(),
        ends_at: end.toISOString(),
      });
      if (!result.ok) {
        toast.error(copy.blockFailed);
        return;
      }
      setBlocks((current) =>
        [...current, result.data].sort((a, b) =>
          a.starts_at.localeCompare(b.starts_at),
        ),
      );
      setBlocksFailed(false);
      setBlockStart("");
      setBlockEnd("");
      toast.success(copy.blockCreated);
    });
  }

  function deleteBlock(block: AvailabilityBlock) {
    if (!canManage || block.source !== "manual") return;
    startTransition(async () => {
      const result = await deleteAvailabilityBlockAction(block.id);
      if (!result.ok) {
        toast.error(copy.blockFailed);
        return;
      }
      setBlocks((current) => current.filter((item) => item.id !== block.id));
      toast.success(copy.blockDeleted);
    });
  }

  function retryAvailability() {
    startTransition(async () => {
      const result = await getMyAvailabilityAction();
      if (!result.ok) return;
      setTimezone(result.data.timezone);
      setIntervals(editableIntervals(result.data.intervals));
      setAvailabilityFailed(false);
    });
  }

  function retryBlocks() {
    startTransition(async () => {
      const result = await listAvailabilityBlocksAction();
      if (!result.ok) return;
      setBlocks(result.data);
      setBlocksFailed(false);
    });
  }

  function retryProviders() {
    startTransition(async () => {
      const result = await listCalendarProvidersAction();
      if (!result.ok) return;
      setProviders(completeProviders(result.data));
      setProvidersFailed(false);
    });
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="text-[18px] font-semibold text-text">{copy.title}</h2>
        <p className="mt-1 text-[12.5px] text-text-muted">{copy.subtitle}</p>
      </div>

      {availabilityFailed || blocksFailed || providersFailed ? (
        <div className="rounded-lg border border-score-mid/30 bg-score-mid/10 px-4 py-3 text-[11.5px] text-score-mid">
          {copy.settingsLoadFailed}
        </div>
      ) : null}

      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <div className="flex flex-col gap-4 border-b border-border pb-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h3 className="text-[13px] font-semibold text-text">{copy.weeklyHours}</h3>
            <p className="mt-1 text-[11px] text-text-faint">{copy.weeklyHint}</p>
          </div>
          <label className="w-full sm:w-[260px]">
            <span className="text-[10.5px] font-medium text-text-muted">{common.timezone}</span>
            <input
              value={timezone}
              onChange={(event) => setTimezone(event.target.value)}
              readOnly={!canManage}
              list="scheduling-timezones"
              className={cn(controlClass, "mt-1.5 font-mono")}
            />
            <datalist id="scheduling-timezones">
              {timezoneOptions.map((zone) => <option key={zone} value={zone} />)}
            </datalist>
            <span className="mt-1 block text-[10px] text-text-faint">{copy.timezoneHint}</span>
          </label>
        </div>

        <div className="divide-y divide-border">
          {common.weekdays.map((day, weekday) => {
            const items = dayIntervals(weekday);
            const enabled = items.length > 0;
            return (
              <div key={day} className="grid gap-3 py-3.5 md:grid-cols-[150px_minmax(0,1fr)]">
                <label className="flex cursor-pointer items-center gap-2 text-[12px] font-medium text-text">
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={(event) => enableDay(weekday, event.target.checked)}
                    disabled={!canManage}
                    className="h-4 w-4 rounded border-border"
                  />
                  {day}
                </label>
                {enabled ? (
                  <div className="flex flex-col gap-2">
                    {items.map((interval) => (
                      <div key={interval.key} className="flex flex-wrap items-center gap-2">
                        <input
                          type="time"
                          value={interval.start_time}
                          onChange={(event) => updateInterval(interval.key, "start_time", event.target.value)}
                          readOnly={!canManage}
                          aria-label={`${day} ${copy.startsAt}`}
                          className="rounded-[6px] border border-border bg-bg px-2.5 py-2 font-mono text-[12px] outline-none focus:border-border-strong"
                        />
                        <span className="text-text-faint">—</span>
                        <input
                          type="time"
                          value={interval.end_time}
                          onChange={(event) => updateInterval(interval.key, "end_time", event.target.value)}
                          readOnly={!canManage}
                          aria-label={`${day} ${copy.endsAt}`}
                          className="rounded-[6px] border border-border bg-bg px-2.5 py-2 font-mono text-[12px] outline-none focus:border-border-strong"
                        />
                        {canManage ? <Button
                          size="icon-sm"
                          variant="ghost"
                          onClick={() => removeInterval(interval.key)}
                          aria-label={copy.removeInterval}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button> : null}
                      </div>
                    ))}
                    {canManage ? <button
                      type="button"
                      onClick={() => addInterval(weekday)}
                      className="inline-flex w-fit items-center gap-1 text-[10.5px] font-medium text-text-muted hover:text-text"
                    >
                      <Plus className="h-3 w-3" />
                      {copy.addInterval}
                    </button> : null}
                  </div>
                ) : (
                  <span className="text-[11.5px] text-text-faint">{copy.unavailable}</span>
                )}
              </div>
            );
          })}
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
          {availabilityFailed ? (
            <Button size="sm" variant="ghost" onClick={retryAvailability} disabled={pending}>
              <RotateCcw className="h-3.5 w-3.5" />
              {common.retry}
            </Button>
          ) : <span />}
          {canManage ? <Button onClick={saveAvailability} disabled={pending}>
            {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            {pending ? copy.savingHours : copy.saveHours}
          </Button> : null}
        </div>
      </section>

      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <h3 className="text-[13px] font-semibold text-text">{copy.manualBlocks}</h3>
        <p className="mt-1 text-[11px] text-text-faint">{copy.blocksHint}</p>
        {canManage ? <form onSubmit={addBlock} className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-[1fr_1fr_auto] lg:items-end">
          <Field label={copy.startsAt}>
            <input type="datetime-local" value={blockStart} onChange={(event) => setBlockStart(event.target.value)} required className={controlClass} />
          </Field>
          <Field label={copy.endsAt}>
            <input type="datetime-local" value={blockEnd} onChange={(event) => setBlockEnd(event.target.value)} required className={controlClass} />
          </Field>
          <Button type="submit" disabled={pending || !blockStart || !blockEnd}>
            {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CalendarOff className="h-4 w-4" />}
            {pending ? copy.addingBlock : copy.addBlock}
          </Button>
        </form> : null}

        {blocksFailed && blocks.length === 0 ? (
          <div className="mt-4 flex items-center justify-between rounded-[7px] border border-border bg-bg px-3 py-3">
            <span className="text-[11.5px] text-text-muted">{copy.blockFailed}</span>
            <Button size="sm" variant="ghost" onClick={retryBlocks} disabled={pending}>{common.retry}</Button>
          </div>
        ) : blocks.length === 0 ? (
          <p className="mt-4 rounded-[7px] border border-dashed border-border px-3 py-5 text-center text-[11.5px] text-text-faint">
            {copy.noBlocks}
          </p>
        ) : (
          <div className="mt-4 divide-y divide-border rounded-[8px] border border-border bg-bg">
            {blocks.map((block) => (
              <div key={block.id} className="flex items-center justify-between gap-3 px-3 py-3">
                <div className="min-w-0">
                  <p className="text-[11.5px] font-medium text-text">
                    {schedulingDateTime(block.starts_at, locale)} — {schedulingDateTime(block.ends_at, locale)}
                  </p>
                  <p className="mt-0.5 text-[10px] text-text-faint">
                    {copy.providerNames[block.source === "manual" ? "local" : block.source]}
                  </p>
                </div>
                {canManage && block.source === "manual" ? (
                  <Button size="icon-sm" variant="ghost" onClick={() => deleteBlock(block)} disabled={pending} aria-label={copy.deleteBlock}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="text-[13px] font-semibold text-text">{copy.providers}</h3>
            <p className="mt-1 max-w-2xl text-[11px] text-text-faint">{copy.providersHint}</p>
          </div>
          {providersFailed ? (
            <Button size="sm" variant="ghost" onClick={retryProviders} disabled={pending}>
              <RotateCcw className="h-3.5 w-3.5" />
              {common.retry}
            </Button>
          ) : null}
        </div>
        <div className="mt-4 grid gap-2 md:grid-cols-3">
          {providers.map((provider) => (
            <div key={provider.connection_id ?? "local"} className="rounded-[8px] border border-border bg-bg px-3.5 py-3">
              <div className="flex items-center justify-between gap-2">
                <span className="inline-flex items-center gap-2 text-[12px] font-medium text-text">
                  {provider.available ? <CalendarCheck className="h-4 w-4 text-primary" /> : <CalendarOff className="h-4 w-4 text-text-faint" />}
                  {provider.provider === "local"
                    ? copy.providerNames.local
                    : provider.name}
                </span>
                <span className={cn(
                  "rounded-[4px] px-2 py-1 text-[10px]",
                  provider.available ? "bg-accent-muted text-primary" : "bg-bg-active text-text-faint",
                )}>
                  {providerStatus(provider)}
                </span>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}


function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-1.5">
      <span className="text-[10.5px] font-medium text-text-muted">{label}</span>
      {children}
    </label>
  );
}


const controlClass =
  "min-h-9 w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[12px] text-text outline-none transition-colors focus:border-border-strong";
