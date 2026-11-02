"use client";

import { useEffect, useState } from "react";
import { Search } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";

const EVENT_NAME = "commandk:toggle";

export function dispatchCommandPaletteOpen() {
  window.dispatchEvent(new CustomEvent(EVENT_NAME));
}

export function useCommandPaletteEvent(handler: () => void) {
  useEffect(() => {
    const listener = () => handler();
    window.addEventListener(EVENT_NAME, listener);
    return () => window.removeEventListener(EVENT_NAME, listener);
  }, [handler]);
}

export function CommandPaletteTrigger({ className }: { className?: string }) {
  const { m } = useI18n();
  const [isMac, setIsMac] = useState(false);

  useEffect(() => {
    setIsMac(/mac/i.test(navigator.platform));
  }, []);

  return (
    <button
      type="button"
      aria-label={m.commandPalette.triggerLabel}
      onClick={() => dispatchCommandPaletteOpen()}
      className={cn(
        "group press inline-flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-2.5 py-1 text-[12px] text-text-muted transition-colors hover:border-border-strong hover:bg-bg-hover hover:text-text",
        className,
      )}
    >
      <Search className="h-3.5 w-3.5 text-text-faint transition-colors group-hover:text-text-muted" />
      <span className="hidden sm:inline">
        {m.commandPalette.triggerLabel}
      </span>
      <kbd className="rounded border border-border bg-bg px-1.5 py-px font-mono text-[10px] text-text-muted transition-colors group-hover:border-border-strong group-hover:text-text">
        {isMac ? "⌘K" : "Ctrl K"}
      </kbd>
    </button>
  );
}
