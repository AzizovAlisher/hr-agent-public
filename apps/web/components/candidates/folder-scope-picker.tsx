"use client";

import { useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

import type { Folder } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useI18n } from "@/components/i18n/locale-provider";
import { folderColor, folderIcon } from "@/components/positions/folder-ui";

/**
 * Compact two-level drill-down picker for choosing a scoring scope by folder.
 * Picking a top-level PARENT (e.g. «Демо Холдинг») selects the whole subtree —
 * the backend scores against the folder and all its children. Drill in to
 * narrow to a single sub-company. Read-only (no editing) — mirrors the
 * dashboard FolderStrip behaviour.
 */
export function FolderScopePicker({
  tree,
  value,
  onChange,
  disabled,
}: {
  tree: Folder[];
  value: string | null;
  onChange: (folderId: string) => void;
  disabled?: boolean;
}) {
  const { m } = useI18n();
  const [parentId, setParentId] = useState<string | null>(null);
  const parent = parentId ? tree.find((f) => f.id === parentId) ?? null : null;
  const items = parent ? parent.children ?? [] : tree;

  return (
    <div className="flex flex-col gap-1.5 rounded-[6px] border border-border bg-bg p-1.5">
      {parent ? (
        <button
          type="button"
          disabled={disabled}
          onClick={() => setParentId(null)}
          className="flex w-fit items-center gap-1 px-1 py-0.5 text-[11px] text-text-muted transition-colors hover:text-text disabled:opacity-50"
        >
          <ChevronLeft className="h-3 w-3" />
          {m.dashboard.folderBack}
        </button>
      ) : null}

      <div className="flex flex-wrap items-center gap-1.5">
        {parent ? (
          <Pill
            label={`${m.dashboard.folderAll} · ${parent.name}`}
            color={parent.color}
            icon={parent.icon}
            count={parent.position_count + parent.descendant_count}
            active={value === parent.id}
            disabled={disabled}
            onClick={() => onChange(parent.id)}
          />
        ) : null}

        {items.map((f) => {
          const isParent = !parent && (f.children?.length ?? 0) > 0;
          return (
            <Pill
              key={f.id}
              label={f.name}
              color={f.color}
              icon={f.icon}
              count={f.position_count + f.descendant_count}
              active={value === f.id}
              hasChildren={isParent}
              disabled={disabled}
              onClick={() => {
                if (isParent) {
                  setParentId(f.id);
                  onChange(f.id); // select the whole subtree by default
                } else {
                  onChange(f.id);
                }
              }}
            />
          );
        })}
      </div>
    </div>
  );
}

function Pill({
  label,
  color,
  icon,
  count,
  active,
  hasChildren,
  disabled,
  onClick,
}: {
  label: string;
  color: string | null;
  icon: string | null;
  count: number;
  active: boolean;
  hasChildren?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  const cls = folderColor(color);
  const Icon = folderIcon(icon);
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[3px] text-[11.5px] font-medium transition-all disabled:opacity-50",
        active
          ? cn(cls.bg, cls.border, cls.text)
          : cn("bg-bg-elevated", cls.border, "text-text-muted hover:text-text"),
      )}
    >
      <Icon className={cn("h-3 w-3", cls.text)} />
      <span className="max-w-[160px] truncate">{label}</span>
      <span className="font-mono text-[10.5px] tabular-nums opacity-70">· {count}</span>
      {hasChildren ? <ChevronRight className="h-3 w-3 opacity-60" /> : null}
    </button>
  );
}
