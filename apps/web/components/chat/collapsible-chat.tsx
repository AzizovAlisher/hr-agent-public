"use client";

import { useEffect, useId, useRef, useState } from "react";
import { MessageSquare, X } from "lucide-react";

import { ChatPanel } from "@/components/chat/chat-panel";
import { useI18n } from "@/components/i18n/locale-provider";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils";

const OPEN_KEY = "hr.chat-panel.open";
const DESKTOP_QUERY = "(min-width: 768px)";

/** @ui-only Collapsible-chat composition properties. */
type Props = {
  scope: "candidate" | "position";
  candidateId?: string;
  positionId?: string;
  scopeRef?: string;
  badge?: number | null;
};

export function CollapsibleChat(props: Props) {
  const { m } = useI18n();
  const [open, setOpen] = useState(false);
  const [desktop, setDesktop] = useState<boolean | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    try {
      setOpen(window.localStorage.getItem(OPEN_KEY) === "1");
    } catch {
      setOpen(false);
    }

    const query = window.matchMedia(DESKTOP_QUERY);
    const update = () => setDesktop(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  function setOpenPersisted(next: boolean) {
    setOpen(next);
    try {
      window.localStorage.setItem(OPEN_KEY, next ? "1" : "0");
    } catch {
      /* Storage is optional; the panel remains fully usable. */
    }
  }

  function closeDesktop() {
    setOpenPersisted(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  }

  if (desktop === null) return null;

  const chat = (
    <ChatPanel
      scope={props.scope}
      candidateId={props.candidateId}
      positionId={props.positionId}
      scopeRef={props.scopeRef}
    />
  );

  if (!desktop) {
    return (
      <Sheet open={open} onOpenChange={setOpenPersisted}>
        <SheetTrigger
          render={
            <button
              type="button"
              aria-label={m.chat.openPanel}
              aria-expanded={open}
              aria-controls={panelId}
              className={cn(
                "fixed right-3 top-[62px] z-40 inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border bg-bg-elevated px-3 py-2 text-[12px] font-medium text-text shadow-md transition hover:border-border-strong hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary",
                open && "pointer-events-none opacity-0",
              )}
            />
          }
        >
          <MessageSquare className="h-4 w-4" />
          <span>{m.chat.openPanel}</span>
          <ChatBadge value={props.badge} />
        </SheetTrigger>
        <SheetContent
          id={panelId}
          side="right"
          showCloseButton={false}
          keepMounted
          className="h-dvh max-h-dvh min-h-0 overflow-hidden gap-0 p-0 data-[side=right]:w-screen data-[side=right]:sm:max-w-none"
        >
          <SheetTitle className="sr-only">
            {props.scope === "candidate"
              ? m.chat.headerCandidate
              : m.chat.headerPosition}
          </SheetTitle>
          <SheetDescription className="sr-only">
            {props.scope === "candidate"
              ? m.chat.emptyDescCandidate
              : m.chat.emptyDescPosition}
          </SheetDescription>
          <PanelHeader
            label={m.chat.close}
            onClose={() => setOpenPersisted(false)}
          />
          <div className="min-h-0 flex-1 overflow-hidden">{chat}</div>
        </SheetContent>
      </Sheet>
    );
  }

  return (
    <>
      {!open ? (
        <button
          ref={triggerRef}
          type="button"
          onClick={() => setOpenPersisted(true)}
          title={m.chat.openPanel}
          aria-label={m.chat.openPanel}
          aria-expanded="false"
          aria-controls={panelId}
          className="fixed right-4 top-[72px] z-30 inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-border bg-bg-elevated px-3 py-1.5 text-[12px] font-medium text-text shadow-sm transition hover:border-border-strong hover:bg-bg-hover"
        >
          <MessageSquare className="h-3.5 w-3.5" />
          <span>{m.chat.openPanel}</span>
          <ChatBadge value={props.badge} />
        </button>
      ) : null}

      <aside
        id={panelId}
        aria-label={
          props.scope === "candidate"
            ? m.chat.headerCandidate
            : m.chat.headerPosition
        }
        className={cn(
          "shrink-0 overflow-hidden border-l border-border bg-background transition-[width] duration-200 ease-out",
          open ? "w-[380px]" : "w-0 border-l-0",
        )}
      >
        <div className="flex h-full w-[380px] flex-col">
          <PanelHeader label={m.chat.close} onClose={closeDesktop} />
          <div className="min-h-0 flex-1">{chat}</div>
        </div>
      </aside>
    </>
  );
}

function PanelHeader({ label, onClose }: { label: string; onClose: () => void }) {
  return (
    <div className="flex h-10 shrink-0 items-center justify-end border-b border-border bg-bg-elevated px-2">
      <button
        type="button"
        onClick={onClose}
        title={label}
        aria-label={label}
        className="flex h-8 w-8 items-center justify-center rounded-md text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

function ChatBadge({ value }: { value?: number | null }) {
  return value != null && value > 0 ? (
    <span className="font-mono text-[10px] tabular-nums text-text-faint">
      · {value}
    </span>
  ) : null;
}
