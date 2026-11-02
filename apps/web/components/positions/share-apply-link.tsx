"use client";

import { useMemo, useState } from "react";
import { Check, Copy, Link as LinkIcon } from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * Recruiter-side widget that builds the public /apply/{id}?utm_source=… URL
 * and copies it to clipboard. Each "channel" choice tags the link so the
 * recruiter can later see which source each candidate came from.
 */
export function ShareApplyLink({ positionId }: { positionId: string }) {
  const { m } = useI18n();
  const t = m.shareApply;
  const [copied, setCopied] = useState<string | null>(null);

  const channels = useMemo<{ source: string; label: string }[]>(
    () => [
      { source: "somon", label: "somon.tj" },
      { source: "telegram", label: "Telegram" },
      { source: "linkedin", label: "LinkedIn" },
      { source: "direct", label: t.channelDirect },
    ],
    [t.channelDirect],
  );

  function buildUrl(source: string): string {
    if (typeof window === "undefined") return "";
    return `${window.location.origin}/apply/${positionId}?utm_source=${source}`;
  }

  async function copyFor(source: string) {
    try {
      await navigator.clipboard.writeText(buildUrl(source));
      setCopied(source);
      setTimeout(() => setCopied(null), 1800);
    } catch {
      // Insecure context / permission denied — tell the user instead of
      // silently doing nothing.
      toast.error(t.copyFailed);
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className={cn(
          "inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-2.5 py-1.5 text-[12px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        )}
      >
        <LinkIcon className="h-3 w-3" />
        {t.button}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-[260px]" sideOffset={6}>
        <div className="px-2 pb-1.5 pt-1 text-[10px] uppercase tracking-[0.08em] text-text-faint">
          {t.dropdownTitle}
        </div>
        <DropdownMenuSeparator />
        {channels.map((c) => (
          <DropdownMenuItem
            key={c.source}
            className="text-xs"
            closeOnClick={false}
            onClick={() => copyFor(c.source)}
          >
            <span className="flex flex-1 items-center justify-between gap-2">
              <span>{c.label}</span>
              {copied === c.source ? (
                <span className="flex items-center gap-1 text-primary">
                  <Check className="h-3 w-3" />
                  {t.copied}
                </span>
              ) : (
                <Copy className="h-3 w-3 text-text-faint" />
              )}
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
