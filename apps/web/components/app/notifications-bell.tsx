"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { Bell, Check } from "lucide-react";

import {
  listNotificationsAction,
  markAllNotificationsReadAction,
  markNotificationReadAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { machineLabel } from "@/components/i18n/machine-label";
import { cn } from "@/lib/utils";
import type { Notification } from "@/lib/types";

const POLL_MS = 30_000;

/**
 * In-app notification bell. Polls the API every 30s for the latest 20 items
 * + unread count, lets the user mark single items or all as read, and links
 * to the candidate that triggered the event.
 */
export function NotificationsBell() {
  const { m } = useI18n();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Notification[]>([]);
  const [unread, setUnread] = useState(0);
  const popoverRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  async function refresh() {
    const res = await listNotificationsAction();
    if (res.ok) {
      setItems(res.data.items);
      setUnread(res.data.unread_count);
    }
  }

  // Initial load + interval polling. Real-time delivery can replace this
  // transport without changing the notification interaction contract.
  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(t);
  }, []);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (!popoverRef.current?.contains(e.target as Node)) setOpen(false);
    }
    window.addEventListener("mousedown", onDoc);
    return () => window.removeEventListener("mousedown", onDoc);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open]);

  async function markOne(id: string) {
    setItems((prev) =>
      prev.map((n) => (n.id === id ? { ...n, read_at: new Date().toISOString() } : n)),
    );
    setUnread((c) => Math.max(0, c - 1));
    await markNotificationReadAction(id);
  }

  async function markAll() {
    setItems((prev) =>
      prev.map((n) =>
        n.read_at ? n : { ...n, read_at: new Date().toISOString() },
      ),
    );
    setUnread(0);
    await markAllNotificationsReadAction();
  }

  return (
    <div ref={popoverRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label={
          unread > 0
            ? `${m.notifications.bellAria} · ${m.notifications.unreadCount(unread)}`
            : m.notifications.bellAria
        }
        aria-expanded={open}
        aria-controls={panelId}
        aria-haspopup="dialog"
        className={cn(
          "relative inline-flex h-7 w-7 items-center justify-center rounded-[6px] text-text-muted transition-colors hover:bg-bg-hover hover:text-text",
          open && "bg-bg-hover text-text",
        )}
      >
        <Bell
          className={cn(
            "h-3.5 w-3.5 transition-transform",
            unread > 0 && "origin-top",
          )}
        />
        {unread > 0 ? (
          <>
            <span
              aria-hidden
              className="pulse-soft absolute -right-0.5 -top-0.5 inline-block h-3.5 w-3.5 rounded-full bg-primary"
              style={{ filter: "blur(2px)", opacity: 0.4 }}
            />
            <span className="absolute -right-0.5 -top-0.5 inline-flex h-3.5 min-w-[14px] items-center justify-center rounded-full bg-primary px-1 font-mono text-[9px] font-semibold tabular-nums text-bg">
              {unread > 99 ? "99+" : unread}
            </span>
          </>
        ) : null}
        <span className="sr-only" aria-live="polite">
          {m.notifications.unreadCount(unread)}
        </span>
      </button>
      {open ? (
        <div
          ref={panelRef}
          id={panelId}
          role="dialog"
          aria-label={m.notifications.title}
          tabIndex={-1}
          className="absolute right-0 top-full z-30 mt-1.5 flex w-[min(340px,calc(100vw-1.5rem))] flex-col rounded-lg border border-border bg-bg-elevated shadow-xl outline-none"
        >
          <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
            <span className="text-[12px] font-semibold tracking-tight">
              {m.notifications.title}
            </span>
            {unread > 0 ? (
              <button
                type="button"
                onClick={markAll}
                className="inline-flex items-center gap-1 rounded-[4px] px-1.5 py-0.5 text-[10.5px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
              >
                <Check className="h-2.5 w-2.5" />
                {m.notifications.markAllRead}
              </button>
            ) : null}
          </div>
          <ul className="max-h-[420px] overflow-y-auto">
            {items.length === 0 ? (
              <li className="px-4 py-8 text-center text-[12px] text-text-faint">
                {m.notifications.empty}
              </li>
            ) : (
              items.map((n) => (
                <NotificationRow
                  key={n.id}
                  notification={n}
                  onClick={() => {
                    if (!n.read_at) void markOne(n.id);
                    setOpen(false);
                  }}
                />
              ))
            )}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function NotificationRow({
  notification,
  onClick,
}: {
  notification: Notification;
  onClick: () => void;
}) {
  const { m, locale } = useI18n();
  const payload = (notification.payload ?? {}) as Record<string, string>;
  const isApply = notification.type === "candidate_applied";
  const isAssignment = notification.type === "candidate_assigned";
  const candidateId = payload.candidate_id;
  const positionId = payload.position_id;
  const positionTitle = payload.position_title ?? "";
  const applicantName = payload.applicant_name ?? "";
  const channel = payload.applied_via ?? "";

  const href = candidateId
    ? `/candidates/${candidateId}${positionId ? `?position=${positionId}` : ""}`
    : "#";
  const ago = formatRel(notification.created_at, locale);

  if (isAssignment) {
    const dueDate = payload.due_date
      ? new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", {
          day: "numeric",
          month: "short",
          year: "numeric",
        }).format(new Date(`${payload.due_date}T12:00:00`))
      : null;
    return (
      <li>
        <Link
          href={href}
          onClick={onClick}
          className={cn(
            "flex flex-col gap-0.5 border-b border-border px-3 py-2.5 text-left transition-colors last:border-b-0 hover:bg-bg-hover",
            !notification.read_at && "bg-accent-muted/30",
          )}
        >
          <div className="flex items-start gap-2">
            <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-primary opacity-80" />
            <div className="min-w-0 flex-1">
              <div className="text-[12.5px] text-text">
                <span className="font-semibold">{m.notifications.assignedToYou}</span>
                {positionTitle ? (
                  <>
                    {" "}
                    <span className="text-text-muted">·</span>{" "}
                    <span className="font-medium">«{positionTitle}»</span>
                  </>
                ) : null}
              </div>
              <div className="mt-0.5 flex items-center gap-2 font-mono text-[10px] text-text-faint">
                <span>{ago}</span>
                {dueDate ? (
                  <>
                    <span>·</span>
                    <span>{m.notifications.dueDate(dueDate)}</span>
                  </>
                ) : null}
              </div>
            </div>
          </div>
        </Link>
      </li>
    );
  }

  if (!isApply) {
    // Fallback rendering for any future notification types we add.
    return (
      <li>
        <button
          type="button"
          onClick={onClick}
          className={cn(
            "flex w-full flex-col items-start gap-0.5 border-b border-border px-3 py-2.5 text-left text-[12px] last:border-b-0 hover:bg-bg-hover",
            !notification.read_at && "bg-accent-muted/30",
          )}
        >
          <span className="text-text">{machineLabel(notification.type, locale)}</span>
          <span className="font-mono text-[10px] text-text-faint">{ago}</span>
        </button>
      </li>
    );
  }

  return (
    <li>
      <Link
        href={href}
        onClick={onClick}
        className={cn(
          "flex flex-col gap-0.5 border-b border-border px-3 py-2.5 text-left transition-colors last:border-b-0 hover:bg-bg-hover",
          !notification.read_at && "bg-accent-muted/30",
        )}
      >
        <div className="flex items-start gap-2">
          <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-primary opacity-80" />
          <div className="min-w-0 flex-1">
            <div className="text-[12.5px] text-text">
              <span className="font-semibold">
                {applicantName || m.notifications.candidateFallback}
              </span>{" "}
              <span className="text-text-muted">
                {m.notifications.appliedTo}{" "}
              </span>
              <span className="font-medium">«{positionTitle}»</span>
            </div>
            <div className="mt-0.5 flex items-center gap-2 font-mono text-[10px] text-text-faint">
              <span>{ago}</span>
              {channel ? (
                <>
                  <span>·</span>
                  <span>{machineLabel(channel, locale)}</span>
                </>
              ) : null}
            </div>
          </div>
        </div>
      </Link>
    </li>
  );
}

function formatRel(iso: string, locale: "ru" | "en"): string {
  const ms = Date.now() - new Date(iso).getTime();
  const sec = Math.max(1, Math.floor(ms / 1000));
  const min = Math.floor(sec / 60);
  const hr = Math.floor(min / 60);
  const day = Math.floor(hr / 24);
  if (locale === "ru") {
    if (sec < 60) return `${sec} сек назад`;
    if (min < 60) return `${min} мин назад`;
    if (hr < 24) return `${hr} ч назад`;
    return `${day} дн назад`;
  }
  if (sec < 60) return `${sec}s ago`;
  if (min < 60) return `${min}m ago`;
  if (hr < 24) return `${hr}h ago`;
  return `${day}d ago`;
}
