"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, useTransition } from "react";
import {
  BriefcaseBusiness,
  CalendarClock,
  Check,
  UserRound,
} from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { updateCandidateWorkAction } from "@/lib/actions";
import type { AssignableUser } from "@/lib/types";

/** @ui-only Work-control composition properties. */
type WorkControlsProps = {
  candidateId: string;
  positionId: string;
  positionTitle: string;
  assignableUsers: AssignableUser[];
  initialOwnerUserId: string | null;
  initialDueDate: string | null;
  canEdit: boolean;
};

export function WorkControls({
  candidateId,
  positionId,
  positionTitle,
  assignableUsers,
  initialOwnerUserId,
  initialDueDate,
  canEdit,
}: WorkControlsProps) {
  const { m } = useI18n();
  const [ownerUserId, setOwnerUserId] = useState(initialOwnerUserId ?? "");
  const [dueDate, setDueDate] = useState(initialDueDate ?? "");
  const [savedOwnerUserId, setSavedOwnerUserId] = useState(
    initialOwnerUserId ?? "",
  );
  const [savedDueDate, setSavedDueDate] = useState(initialDueDate ?? "");
  const [didSave, setDidSave] = useState(false);
  const [pending, startSave] = useTransition();

  const ownerIsAvailable = useMemo(
    () =>
      !ownerUserId || assignableUsers.some((user) => user.id === ownerUserId),
    [assignableUsers, ownerUserId],
  );
  const isDirty =
    ownerUserId !== savedOwnerUserId || dueDate !== savedDueDate;

  useEffect(() => {
    const nextOwner = initialOwnerUserId ?? "";
    const nextDue = initialDueDate ?? "";
    setOwnerUserId(nextOwner);
    setDueDate(nextDue);
    setSavedOwnerUserId(nextOwner);
    setSavedDueDate(nextDue);
    setDidSave(false);
  }, [initialOwnerUserId, initialDueDate]);

  function save() {
    startSave(async () => {
      const updates: Parameters<typeof updateCandidateWorkAction>[2] = {};
      if (ownerUserId !== savedOwnerUserId) {
        updates.owner_user_id = ownerUserId || null;
        updates.expected_owner_user_id = savedOwnerUserId || null;
      }
      if (dueDate !== savedDueDate) {
        updates.next_action_due_date = dueDate || null;
        updates.expected_next_action_due_date = savedDueDate || null;
      }
      const result = await updateCandidateWorkAction(
        positionId,
        candidateId,
        updates,
      );

      if (!result.ok) {
        toast.error(m.myWork.saveFailed(result.error));
        return;
      }

      const savedOwner = result.data.owner?.id ?? "";
      const savedDue = result.data.next_action_due_date ?? "";
      setOwnerUserId(savedOwner);
      setDueDate(savedDue);
      setSavedOwnerUserId(savedOwner);
      setSavedDueDate(savedDue);
      setDidSave(true);
    });
  }

  return (
    <section className="rounded-lg border border-border bg-bg-elevated">
      <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-end">
        <div className="min-w-0 flex-1 sm:max-w-[280px]">
          <div className="mb-2 flex min-w-0 items-center gap-2">
            <BriefcaseBusiness className="h-3.5 w-3.5 shrink-0 text-text-faint" />
            <Link
              href={`/positions/${positionId}`}
              className="truncate text-[12px] font-medium text-text transition-colors hover:text-primary"
            >
              {positionTitle}
            </Link>
          </div>
          <label className="block">
            <span className="mb-1 flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              <UserRound className="h-3 w-3" />
              {m.myWork.ownerLabel}
            </span>
            <select
              value={ownerUserId}
              onChange={(event) => {
                setOwnerUserId(event.target.value);
                setDidSave(false);
              }}
              disabled={pending || !canEdit}
              className="h-8 w-full rounded-[6px] border border-border bg-bg px-2.5 text-[12px] text-text outline-none transition-colors focus:border-border-strong disabled:opacity-60"
            >
              <option value="">{m.myWork.unassigned}</option>
              {!ownerIsAvailable && ownerUserId ? (
                <option value={ownerUserId}>—</option>
              ) : null}
              {assignableUsers.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.email}
                </option>
              ))}
            </select>
          </label>
        </div>

        <label className="block min-w-0 flex-1 sm:max-w-[210px]">
          <span className="mb-1 flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            <CalendarClock className="h-3 w-3" />
            {m.myWork.dueLabel}
          </span>
          <input
            type="date"
            value={dueDate}
            onChange={(event) => {
              setDueDate(event.target.value);
              setDidSave(false);
            }}
            disabled={pending || !canEdit}
            className="h-8 w-full rounded-[6px] border border-border bg-bg px-2.5 font-mono text-[12px] text-text outline-none transition-colors focus:border-border-strong disabled:opacity-60"
          />
        </label>

        <div className="flex h-8 shrink-0 items-center gap-2">
          {canEdit && didSave && !isDirty ? (
            <span
              className="inline-flex items-center gap-1 text-[11px] text-text-muted"
              role="status"
            >
              <Check className="h-3 w-3" />
              {m.myWork.saved}
            </span>
          ) : null}
          {canEdit ? (
            <button
              type="button"
              onClick={save}
              disabled={pending || !isDirty}
              className="inline-flex h-8 items-center rounded-[6px] bg-text px-3 text-[11px] font-medium text-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {pending ? m.myWork.saving : m.myWork.save}
            </button>
          ) : null}
        </div>
      </div>
    </section>
  );
}
