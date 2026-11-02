"use client";

import { useState } from "react";

import { useI18n } from "@/components/i18n/locale-provider";
import { categorizeAll, CATEGORY_CLASSES } from "@/lib/requirement-category";
import { cn } from "@/lib/utils";

/** @ui-only Requirement-chip presentation properties. */
type Props = {
  requirements: Record<string, unknown> | null | undefined;
  locale?: "ru" | "en";
  /** Show at most N chips until the reader expands the rest. */
  max?: number;
  /** Visual size — "sm" for cards, "md" for headers. */
  size?: "sm" | "md";
  className?: string;
};

/**
 * Render position requirements as colour-coded pills. Skips ingest metadata
 * (source_url, applicant_*, etc.). Categories: salary, location, format,
 * seniority, experience, language, education, tech, other.
 *
 * The overflow pill is a real button rather than a dead "+N" label. That
 * matters more than it looks: `categorizeAll` sorts by category rank and tech
 * sorts second-to-last, so a capped list hides precisely the stack — the part
 * a candidate or recruiter most wants to see.
 *
 * Client component so the toggle can hold state. Its one server-component call
 * site (the public /apply page) passes only serializable props.
 */
export function RequirementChips({
  requirements,
  locale = "ru",
  max,
  size = "sm",
  className,
}: Props) {
  const { m } = useI18n();
  const [expanded, setExpanded] = useState(false);

  const items = categorizeAll(requirements, locale);
  if (items.length === 0) return null;

  const capped = max != null && items.length > max;
  const visible = capped && !expanded ? items.slice(0, max) : items;
  const hidden = capped ? items.length - max : 0;

  const sizeCls =
    size === "sm"
      ? "px-2 py-[2px] text-[10.5px] max-w-[180px]"
      : "px-2.5 py-[3px] text-[11.5px] max-w-[260px]";

  return (
    <ul className={cn("flex w-full min-w-0 flex-wrap gap-1.5", className)}>
      {visible.map((it) => (
        <li
          key={`${it.category}:${it.key}:${it.value}`}
          className={cn(
            // Hard max-w on the chip itself — so even if a label slips past
            // MAX_LABEL (e.g. user typed a verbose value in the editor), one
            // chip can never claim the whole row. overflow-hidden + the inner
            // truncate keep the label single-line with an ellipsis.
            "inline-flex items-center overflow-hidden rounded-full border font-medium leading-tight",
            sizeCls,
            CATEGORY_CLASSES[it.category],
          )}
          title={`${it.key}: ${it.value}`}
        >
          <span className="truncate">{it.label}</span>
        </li>
      ))}
      {capped ? (
        <li className="inline-flex shrink-0">
          <button
            type="button"
            aria-expanded={expanded}
            onClick={(e) => {
              // Cards render these chips inside a <Link> that is also a
              // dnd-kit drag source — without this the click navigates away
              // or starts a drag instead of expanding.
              e.preventDefault();
              e.stopPropagation();
              setExpanded((v) => !v);
            }}
            className={cn(
              "inline-flex shrink-0 cursor-pointer items-center rounded-full border border-border bg-bg-elevated font-mono text-text-faint transition-colors hover:border-border-strong hover:text-text",
              sizeCls,
            )}
          >
            {expanded ? m.common.chipsShowLess : m.common.chipsShowMore(hidden)}
          </button>
        </li>
      ) : null}
    </ul>
  );
}
