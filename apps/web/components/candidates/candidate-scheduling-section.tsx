"use client";

import {
  CalendarClock,
  Check,
  Clipboard,
  Link2,
  Loader2,
  MapPin,
  Plus,
  RotateCcw,
  UserRound,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  createCandidateSchedulingLinkAction,
  getSchedulingPermissionsAction,
  listCalendarProvidersAction,
  listCandidateAppointmentsAction,
  listCandidateSchedulingLinksAction,
  revokeSchedulingLinkAction,
  rotateSchedulingLinkAction,
} from "@/lib/actions";
import type {
  AssignableUser,
  CalendarProvider,
  Interview,
  InterviewAppointment,
  SchedulingLink,
  SchedulingLocationMode,
} from "@/lib/types";
import { schedulingDateTime } from "@/lib/scheduling-time";
import { cn } from "@/lib/utils";


/** @ui-only Select-option projection for scheduling. */
type PositionOption = { id: string; title: string };


function publicSchedulingPath(publicUrl: string): string | null {
  const match = publicUrl.match(/^\/public\/scheduling\/([A-Za-z0-9_-]+)$/);
  return match ? `/schedule/${encodeURIComponent(match[1])}` : null;
}


export function CandidateSchedulingSection({
  candidateId,
  positions,
  interviews,
  defaultPositionId,
  assignableUsers,
  currentUserId,
  initialLinks,
  initialAppointments,
  initialLinksFailed,
  initialAppointmentsFailed,
}: {
  candidateId: string;
  positions: PositionOption[];
  interviews: Interview[];
  defaultPositionId: string | null;
  assignableUsers: AssignableUser[];
  currentUserId: string;
  initialLinks: SchedulingLink[];
  initialAppointments: InterviewAppointment[];
  initialLinksFailed: boolean;
  initialAppointmentsFailed: boolean;
}) {
  const { locale, m } = useI18n();
  const copy = m.scheduling.candidate;
  const common = m.scheduling.common;
  const [showForm, setShowForm] = useState(false);
  const [links, setLinks] = useState(initialLinks);
  const [appointments, setAppointments] = useState(initialAppointments);
  const [linksFailed, setLinksFailed] = useState(initialLinksFailed);
  const [appointmentsFailed, setAppointmentsFailed] = useState(
    initialAppointmentsFailed,
  );
  const [positionId, setPositionId] = useState(
    positions.some((position) => position.id === defaultPositionId)
      ? (defaultPositionId ?? "")
      : (positions[0]?.id ?? ""),
  );
  const eligibleInterviews = useMemo(
    () =>
      interviews.filter(
        (interview) =>
          interview.position_id === positionId &&
          interview.status !== "archived" &&
          interview.status !== "scored",
      ),
    [interviews, positionId],
  );
  const [interviewId, setInterviewId] = useState(
    interviews.find(
      (interview) =>
        interview.position_id ===
          (positions.some((position) => position.id === defaultPositionId)
            ? defaultPositionId
            : positions[0]?.id) &&
        interview.status !== "archived" &&
        interview.status !== "scored",
    )?.id ?? "",
  );
  const defaultInterviewer = assignableUsers.some(
    (user) => user.id === currentUserId,
  )
    ? currentUserId
    : (assignableUsers[0]?.id ?? "");
  const [interviewerIds, setInterviewerIds] = useState<string[]>(
    defaultInterviewer ? [defaultInterviewer] : [],
  );
  const [duration, setDuration] = useState(60);
  const [minNotice, setMinNotice] = useState(120);
  const [bookingWindow, setBookingWindow] = useState(14);
  const [buffer, setBuffer] = useState(0);
  const [expiresIn, setExpiresIn] = useState(14);
  const [locationMode, setLocationMode] =
    useState<SchedulingLocationMode>("video");
  const [locationDetails, setLocationDetails] = useState("");
  const [calendarConnectionId, setCalendarConnectionId] = useState("local");
  const [calendarProviders, setCalendarProviders] = useState<CalendarProvider[]>([
    {
      provider: "local",
      connection_id: null,
      name: m.scheduling.settings.providerNames.local,
      status: null,
      capability: "local",
      available: true,
      reason: null,
    },
  ]);
  const [calendarProvidersFailed, setCalendarProvidersFailed] = useState(false);
  const [canManage, setCanManage] = useState(false);
  const [pending, startTransition] = useTransition();
  const positionById = useMemo(
    () => new Map(positions.map((position) => [position.id, position.title])),
    [positions],
  );

  useEffect(() => {
    if (eligibleInterviews.some((interview) => interview.id === interviewId)) return;
    setInterviewId(eligibleInterviews[0]?.id ?? "");
  }, [eligibleInterviews, interviewId]);

  useEffect(() => {
    let active = true;
    void listCalendarProvidersAction().then((result) => {
      if (!active) return;
      if (!result.ok) {
        setCalendarProvidersFailed(true);
        return;
      }
      setCalendarProviders(result.data.filter((provider) => provider.available));
      setCalendarProvidersFailed(false);
    });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    void getSchedulingPermissionsAction().then((result) => {
      if (!active) return;
      setCanManage(
        result.ok && result.data.permissions["scheduling.manage"] === true,
      );
    });
    return () => {
      active = false;
    };
  }, []);

  function toggleInterviewer(userId: string) {
    setInterviewerIds((current) =>
      current.includes(userId)
        ? current.filter((id) => id !== userId)
        : [...current, userId],
    );
  }

  function createLink(event: React.FormEvent) {
    event.preventDefault();
    if (!positionId || interviewerIds.length === 0 || pending) return;
    startTransition(async () => {
      const result = await createCandidateSchedulingLinkAction(candidateId, {
        position_id: positionId,
        interview_id: interviewId || null,
        interviewer_user_ids: interviewerIds,
        calendar_connection_id:
          calendarConnectionId === "local" ? null : calendarConnectionId,
        duration_minutes: duration,
        slot_interval_minutes: 30,
        min_notice_minutes: minNotice,
        booking_window_days: bookingWindow,
        buffer_minutes: buffer,
        location_mode: locationMode,
        location_details: locationDetails.trim() || null,
        expires_in_days: expiresIn,
        reminder_offsets: [1440, 60],
      });
      if (!result.ok) {
        toast.error(copy.createFailed);
        return;
      }
      setLinks((current) => [
        result.data,
        ...current.filter((link) => link.id !== result.data.id),
      ]);
      setLinksFailed(false);
      setShowForm(false);
      toast.success(copy.linkCreated);
    });
  }

  function revoke(link: SchedulingLink) {
    if (pending || !window.confirm(copy.revokeConfirm)) return;
    startTransition(async () => {
      const result = await revokeSchedulingLinkAction(link.id, candidateId);
      if (!result.ok) {
        toast.error(copy.createFailed);
        return;
      }
      setLinks((current) =>
        current.map((item) =>
          item.id === link.id
            ? {
                ...item,
                revoked_at: new Date().toISOString(),
                public_url: null,
              }
            : item,
        ),
      );
      toast.success(copy.revoked);
    });
  }

  async function copyLink(link: SchedulingLink) {
    const path = link.public_url
      ? publicSchedulingPath(link.public_url)
      : null;
    if (!path) {
      toast.error(copy.copyFailed);
      return;
    }
    try {
      await navigator.clipboard.writeText(new URL(path, window.location.origin).toString());
      toast.success(copy.copied);
    } catch {
      toast.error(copy.copyFailed);
    }
  }

  function rotate(link: SchedulingLink) {
    if (pending || !window.confirm(copy.rotateConfirm)) return;
    startTransition(async () => {
      const result = await rotateSchedulingLinkAction(link.id);
      if (!result.ok) {
        toast.error(copy.rotateFailed);
        return;
      }
      setLinks((current) =>
        current.map((item) => (item.id === link.id ? result.data : item)),
      );
      toast.success(copy.rotated);
    });
  }

  function retryLinks() {
    startTransition(async () => {
      const result = await listCandidateSchedulingLinksAction(candidateId);
      if (!result.ok) {
        setLinksFailed(true);
        return;
      }
      setLinks(result.data);
      setLinksFailed(false);
    });
  }

  function retryAppointments() {
    startTransition(async () => {
      const result = await listCandidateAppointmentsAction(candidateId);
      if (!result.ok) {
        setAppointmentsFailed(true);
        return;
      }
      setAppointments(result.data);
      setAppointmentsFailed(false);
    });
  }

  return (
    <section className="overflow-hidden rounded-lg border border-border bg-bg-elevated">
      <div className="flex flex-col gap-3 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-[13px] font-semibold text-text">{copy.title}</h2>
          <p className="mt-0.5 text-[11px] text-text-faint">{copy.subtitle}</p>
        </div>
        {canManage ? (
          <Button
            size="sm"
            variant={showForm ? "ghost" : "outline"}
            onClick={() => setShowForm((current) => !current)}
            disabled={positions.length === 0}
          >
            {showForm ? <X className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
            {showForm ? m.common.cancel : copy.createLink}
          </Button>
        ) : null}
      </div>

      {positions.length === 0 ? (
        <p className="border-b border-border px-4 py-4 text-[12px] text-text-muted">
          {copy.noEligiblePositions}
        </p>
      ) : null}

      {showForm && canManage ? (
        <form onSubmit={createLink} className="border-b border-border bg-bg/35 px-4 py-4">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <Field label={copy.position}>
              <select
                value={positionId}
                onChange={(event) => setPositionId(event.target.value)}
                className={controlClass}
              >
                {positions.map((position) => (
                  <option key={position.id} value={position.id}>
                    {position.title}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={copy.interviewKit}>
              <select
                value={interviewId}
                onChange={(event) => setInterviewId(event.target.value)}
                className={controlClass}
              >
                <option value="">{copy.noInterviewKit}</option>
                {eligibleInterviews.map((interview) => (
                  <option key={interview.id} value={interview.id}>
                    {copy.interviewKitVersion(interview.kit_version)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={copy.duration}>
              <NumberSelect value={duration} onChange={setDuration} values={[30, 45, 60, 90]} label={common.minutes} />
            </Field>
            <Field label={copy.minNotice}>
              <NumberSelect value={minNotice} onChange={setMinNotice} values={[0, 60, 120, 1440]} label={common.minutes} />
            </Field>
            <Field label={copy.bookingWindow}>
              <NumberSelect value={bookingWindow} onChange={setBookingWindow} values={[7, 14, 30]} label={common.days} />
            </Field>
            <Field label={copy.buffer}>
              <NumberSelect value={buffer} onChange={setBuffer} values={[0, 15, 30]} label={common.minutes} />
            </Field>
            <Field label={copy.expiresIn}>
              <NumberSelect value={expiresIn} onChange={setExpiresIn} values={[7, 14, 30]} label={common.days} />
            </Field>
            <Field label={copy.location}>
              <select
                value={locationMode}
                onChange={(event) =>
                  setLocationMode(event.target.value as SchedulingLocationMode)
                }
                className={controlClass}
              >
                {(["video", "phone", "onsite"] as const).map((mode) => (
                  <option key={mode} value={mode}>
                    {common.locationModes[mode]}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={copy.locationDetails}>
              <input
                value={locationDetails}
                onChange={(event) => setLocationDetails(event.target.value)}
                placeholder={copy.locationPlaceholder}
                maxLength={2000}
                className={controlClass}
              />
            </Field>
            <Field label={copy.calendarProvider}>
              <select
                value={calendarConnectionId}
                onChange={(event) => setCalendarConnectionId(event.target.value)}
                className={controlClass}
              >
                {calendarProviders.map((provider) => (
                  <option
                    key={provider.connection_id ?? "local"}
                    value={provider.connection_id ?? "local"}
                  >
                    {provider.name}
                  </option>
                ))}
              </select>
              {calendarProvidersFailed ? (
                <span className="text-[10px] text-score-mid">
                  {copy.calendarProvidersFailed}
                </span>
              ) : null}
            </Field>
          </div>

          <fieldset className="mt-4">
            <legend className="text-[11px] font-medium text-text-muted">
              {copy.interviewers}
            </legend>
            <div className="mt-2 flex flex-wrap gap-2">
              {assignableUsers.map((user) => {
                const selected = interviewerIds.includes(user.id);
                return (
                  <label
                    key={user.id}
                    className={cn(
                      "flex cursor-pointer items-center gap-2 rounded-[6px] border px-2.5 py-2 text-[11.5px] transition-colors",
                      selected
                        ? "border-accent-border bg-accent-muted text-text"
                        : "border-border bg-bg-elevated text-text-muted hover:border-border-strong",
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={() => toggleInterviewer(user.id)}
                      className="sr-only"
                    />
                    <span className="flex h-4 w-4 items-center justify-center rounded-[4px] border border-border-strong">
                      {selected ? <Check className="h-3 w-3" /> : null}
                    </span>
                    {user.email}
                  </label>
                );
              })}
            </div>
            {interviewerIds.length === 0 ? (
              <p className="mt-2 text-[11px] text-destructive">{copy.selectInterviewer}</p>
            ) : null}
            <p className="mt-2 text-[10.5px] text-text-faint">{copy.availabilityHint}</p>
          </fieldset>

          <div className="mt-4 flex justify-end">
            <Button type="submit" size="sm" disabled={pending || !positionId || interviewerIds.length === 0}>
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Link2 className="h-3.5 w-3.5" />}
              {pending ? copy.creatingLink : copy.createLink}
            </Button>
          </div>
        </form>
      ) : null}

      <div className="grid divide-y divide-border xl:grid-cols-2 xl:divide-x xl:divide-y-0">
        <div className="min-w-0 px-4 py-4">
          <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-[0.08em] text-text-muted">
            {copy.links}
          </h3>
          {linksFailed && links.length === 0 ? (
            <RetryState label={copy.linksLoadFailed} pending={pending} onRetry={retryLinks} retry={common.retry} />
          ) : links.length === 0 ? (
            <p className="py-4 text-center text-[12px] text-text-muted">{copy.noLinks}</p>
          ) : (
            <div className="flex flex-col gap-2">
              {links.map((link) => (
                <SchedulingLinkRow
                  key={link.id}
                  link={link}
                  positionTitle={positionById.get(link.position_id) ?? null}
                  pending={pending}
                  canManage={canManage}
                  onCopy={() => copyLink(link)}
                  onRotate={() => rotate(link)}
                  onRevoke={() => revoke(link)}
                />
              ))}
              {linksFailed ? (
                <p className="text-[10.5px] text-destructive">{copy.linksLoadFailed}</p>
              ) : null}
            </div>
          )}
        </div>

        <div className="min-w-0 px-4 py-4">
          <div className="mb-3">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-muted">
              {copy.appointments}
            </h3>
            <p className="mt-1 text-[10.5px] text-text-faint">{copy.appointmentsHint}</p>
          </div>
          {appointmentsFailed && appointments.length === 0 ? (
            <RetryState label={copy.appointmentsLoadFailed} pending={pending} onRetry={retryAppointments} retry={common.retry} />
          ) : appointments.length === 0 ? (
            <p className="py-4 text-center text-[12px] text-text-muted">{copy.noAppointments}</p>
          ) : (
            <div className="flex flex-col gap-2">
              {appointments.map((appointment) => (
                <AppointmentRow key={appointment.id} appointment={appointment} />
              ))}
              {appointmentsFailed ? (
                <p className="text-[10.5px] text-destructive">{copy.appointmentsLoadFailed}</p>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}


function SchedulingLinkRow({
  link,
  positionTitle,
  pending,
  canManage,
  onCopy,
  onRotate,
  onRevoke,
}: {
  link: SchedulingLink;
  positionTitle: string | null;
  pending: boolean;
  canManage: boolean;
  onCopy: () => void;
  onRotate: () => void;
  onRevoke: () => void;
}) {
  const { locale, m } = useI18n();
  const copy = m.scheduling.candidate;
  const expired = new Date(link.expires_at).getTime() <= Date.now();
  const status = link.revoked_at
    ? "revoked"
    : link.booked_at
      ? "booked"
      : expired
        ? "expired"
        : "active";
  const canUse = status === "active";
  return (
    <div className="rounded-[8px] border border-border bg-bg px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-[12.5px] font-medium text-text">
            {positionTitle ?? link.position_id}
          </p>
          <p className="mt-1 text-[10.5px] text-text-faint">
            {m.scheduling.common.minutes(link.duration_minutes)} · {copy.participants(link.interviewer_user_ids.length)}
          </p>
        </div>
        <span className={cn(
          "rounded-[4px] px-2 py-1 text-[10px] font-medium",
          status === "active" ? "bg-accent-muted text-primary" : "bg-bg-active text-text-muted",
        )}>
          {copy[status]}
        </span>
      </div>
      <p className="mt-2 text-[10.5px] text-text-faint">
        {copy.expiresAt(schedulingDateTime(link.expires_at, locale))} ·{" "}
        {m.scheduling.settings.providerNames[link.provider]}
      </p>
      {canManage && canUse ? (
        <>
          <p className="mt-2 text-[10.5px] text-text-muted">
            {link.public_url ? copy.linkAvailableOnce : copy.linkRedacted}
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {link.public_url ? (
              <Button size="sm" variant="outline" onClick={onCopy} disabled={pending}>
                <Clipboard className="h-3.5 w-3.5" />
                {copy.copyLink}
              </Button>
            ) : null}
            <Button size="sm" variant="outline" onClick={onRotate} disabled={pending}>
              <RotateCcw className="h-3.5 w-3.5" />
              {copy.rotateLink}
            </Button>
            <Button size="sm" variant="ghost" onClick={onRevoke} disabled={pending}>
              {copy.revoke}
            </Button>
          </div>
        </>
      ) : null}
    </div>
  );
}


function AppointmentRow({ appointment }: { appointment: InterviewAppointment }) {
  const { locale, m } = useI18n();
  const copy = m.scheduling.candidate;
  const common = m.scheduling.common;
  const providerName = m.scheduling.settings.providerNames[appointment.provider];
  return (
    <div className="rounded-[8px] border border-border bg-bg px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-[12.5px] font-medium text-text">
            {appointment.position_title}
          </p>
          <p className="mt-1 flex items-center gap-1.5 text-[11px] text-text-muted">
            <CalendarClock className="h-3.5 w-3.5" />
            {schedulingDateTime(appointment.starts_at, locale, appointment.candidate_timezone)}
          </p>
        </div>
        <span className="rounded-[4px] bg-bg-active px-2 py-1 text-[10px] font-medium text-text-muted">
          {common.appointmentStatuses[appointment.status]}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10.5px] text-text-faint">
        <span className="inline-flex items-center gap-1">
          <MapPin className="h-3 w-3" />
          {common.locationModes[appointment.location_mode]}
        </span>
        <span className="inline-flex items-center gap-1">
          <UserRound className="h-3 w-3" />
          {copy.participants(appointment.interviewer_labels.length)}
        </span>
        <span>{copy.providerSync(providerName, common.syncStatuses[appointment.sync_status])}</span>
      </div>
      {appointment.location_details ? (
        <p className="mt-2 break-words text-[10.5px] text-text-muted">
          {appointment.location_details}
        </p>
      ) : null}
    </div>
  );
}


function RetryState({
  label,
  pending,
  onRetry,
  retry,
}: {
  label: string;
  pending: boolean;
  onRetry: () => void;
  retry: string;
}) {
  return (
    <div className="flex flex-col items-center gap-2 py-5 text-center">
      <p className="text-[11.5px] text-text-muted">{label}</p>
      <Button size="sm" variant="outline" onClick={onRetry} disabled={pending}>
        {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
        {retry}
      </Button>
    </div>
  );
}


function NumberSelect({
  value,
  onChange,
  values,
  label,
}: {
  value: number;
  onChange: (value: number) => void;
  values: number[];
  label: (value: number) => string;
}) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(Number(event.target.value))}
      className={controlClass}
    >
      {values.map((option) => (
        <option key={option} value={option}>
          {label(option)}
        </option>
      ))}
    </select>
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
  "min-h-9 w-full rounded-[6px] border border-border bg-bg-elevated px-2.5 py-2 text-[12px] text-text outline-none transition-colors focus:border-border-strong";
