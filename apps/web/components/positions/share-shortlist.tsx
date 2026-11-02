"use client";

import { useEffect, useState, useTransition } from "react";
import {
  Check,
  Copy,
  Link as LinkIcon,
  RefreshCw,
  Share2,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  createShortlistAction,
  listShortlistsAction,
  revokeShortlistAction,
  rotateShortlistAction,
} from "@/lib/actions";
import type { ShortlistLink } from "@/lib/types";
import { cn } from "@/lib/utils";

const TOP_N_OPTIONS = [3, 5, 10, 20];

export function ShareShortlistButton({
  positionId,
  canIncludeContact,
}: {
  positionId: string;
  canIncludeContact: boolean;
}) {
  const { m } = useI18n();
  const t = m.shortlist;

  const [open, setOpen] = useState(false);
  const [links, setLinks] = useState<ShortlistLink[]>([]);
  const [topN, setTopN] = useState<number>(5);
  const [includeContact, setIncludeContact] = useState(false);
  const [pending, startCreate] = useTransition();
  const [copiedToken, setCopiedToken] = useState<string | null>(null);
  const [freshTokens, setFreshTokens] = useState<Record<string, string>>({});
  const [revoking, setRevoking] = useState<string | null>(null);

  // Load existing links each time dialog opens
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    listShortlistsAction(positionId).then((res) => {
      if (cancelled) return;
      if (res.ok) setLinks(res.data);
    });
    return () => {
      cancelled = true;
    };
  }, [open, positionId]);

  const activeLinks = links.filter(
    (l) => l.revoked_at == null && new Date(l.expires_at).getTime() > Date.now(),
  );

  function buildPublicUrl(token: string): string {
    if (typeof window === "undefined") return "";
    return `${window.location.origin}/shortlist/${token}`;
  }

  async function copy(token: string) {
    try {
      await navigator.clipboard.writeText(buildPublicUrl(token));
      setCopiedToken(token);
      toast.success(t.copied);
      setTimeout(() => setCopiedToken(null), 1500);
    } catch {
      toast.error(t.failedToast("clipboard"));
    }
  }

  function generate() {
    startCreate(async () => {
      const res = await createShortlistAction(positionId, {
        top_n: topN,
        include_contact: includeContact,
      });
      if (!res.ok) {
        toast.error(t.failedToast(res.error));
        return;
      }
      setLinks((prev) => [res.data, ...prev]);
      setFreshTokens((prev) => ({ ...prev, [res.data.id]: res.data.token }));
      toast.success(t.createdToast);
      // Auto-copy the freshly minted link — recruiter wanted it, give it to them.
      void copy(res.data.token);
    });
  }

  async function rotate(link: ShortlistLink) {
    setRevoking(link.id);
    const res = await rotateShortlistAction(link.id, positionId);
    setRevoking(null);
    if (!res.ok) {
      toast.error(t.failedToast(res.error));
      return;
    }
    setLinks((prev) =>
      prev.map((item) => (item.id === link.id ? res.data : item)),
    );
    setFreshTokens((prev) => ({ ...prev, [link.id]: res.data.token }));
    toast.success(t.createdToast);
    void copy(res.data.token);
  }

  async function revoke(link: ShortlistLink) {
    if (!confirm(t.revokeConfirm)) return;
    setRevoking(link.id);
    const res = await revokeShortlistAction(link.id, positionId);
    setRevoking(null);
    if (!res.ok) {
      toast.error(t.failedToast(res.error));
      return;
    }
    setLinks((prev) =>
      prev.map((l) => (l.id === link.id ? res.data : l)),
    );
    toast.success(t.revokedToast);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        className={cn(
          "inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-2.5 py-1.5 text-[12px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        )}
      >
        <Share2 className="h-3 w-3" />
        {t.shareButton}
      </DialogTrigger>
      <DialogContent
        className="max-w-[520px] gap-0 p-0"
        showCloseButton={false}
      >
        <div className="flex flex-col gap-1 border-b border-border px-5 py-4">
          <DialogTitle className="text-[14px] tracking-tight">
            {t.dialogTitle}
          </DialogTitle>
          <DialogDescription className="text-[12px] text-text-muted">
            {t.dialogDesc}
          </DialogDescription>
        </div>

        {/* Config */}
        <div className="flex flex-col gap-4 px-5 py-4">
          {canIncludeContact ? <div className="flex flex-col gap-2">
            <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {t.topNLabel}
            </span>
            <div className="flex items-center gap-1.5">
              {TOP_N_OPTIONS.map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => setTopN(n)}
                  className={cn(
                    "rounded-md border px-3 py-1.5 text-[12px] font-medium transition-colors",
                    topN === n
                      ? "border-border-strong bg-bg-active text-text"
                      : "border-border bg-bg-elevated text-text-muted hover:border-border-strong hover:text-text",
                  )}
                >
                  {n}
                </button>
              ))}
            </div>
            <span className="text-[11px] text-text-faint">{t.topNHint}</span>
          </div> : null}

          <div className="flex flex-col gap-2">
            <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {t.includeContactLabel}
            </span>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={() => setIncludeContact(false)}
                className={cn(
                  "rounded-md border px-3 py-1.5 text-[12px] font-medium transition-colors",
                  !includeContact
                    ? "border-border-strong bg-bg-active text-text"
                    : "border-border bg-bg-elevated text-text-muted hover:border-border-strong hover:text-text",
                )}
              >
                {t.includeContactOff}
              </button>
              <button
                type="button"
                onClick={() => setIncludeContact(true)}
                className={cn(
                  "rounded-md border px-3 py-1.5 text-[12px] font-medium transition-colors",
                  includeContact
                    ? "border-border-strong bg-bg-active text-text"
                    : "border-border bg-bg-elevated text-text-muted hover:border-border-strong hover:text-text",
                )}
              >
                {t.includeContactOn}
              </button>
            </div>
            <span className="text-[11px] text-text-faint">
              {t.includeContactHint}
            </span>
          </div>

          <button
            type="button"
            onClick={generate}
            disabled={pending}
            className="inline-flex items-center justify-center gap-2 rounded-md bg-text px-3 py-2 text-[13px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            <LinkIcon className="h-3.5 w-3.5" />
            {pending ? t.generating : t.generate}
          </button>
        </div>

        {/* Existing links */}
        <div className="border-t border-border bg-bg/40 px-5 py-4">
          <div className="mb-2 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {t.activeLinks}
          </div>
          {activeLinks.length === 0 ? (
            <p className="text-[12px] text-text-muted">{t.noLinks}</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {activeLinks.map((l) => {
                const token = freshTokens[l.id];
                const url = token ? buildPublicUrl(token) : null;
                return (
                  <li
                    key={l.id}
                    className="flex flex-col gap-1.5 rounded-md border border-border bg-bg-elevated px-3 py-2.5"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-[11.5px] text-text-muted">
                        {t.topNSummary(l.top_n)}
                        {l.include_contact ? ` · ${t.includeContactOn.toLowerCase()}` : ""}
                      </span>
                      <div className="flex items-center gap-1.5">
                        {token ? (
                          <button
                            type="button"
                            onClick={() => copy(token)}
                            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-[11px] text-text-muted transition-colors hover:border-border-strong hover:text-text"
                          >
                            {copiedToken === token ? (
                              <>
                                <Check className="h-3 w-3 text-primary" />
                                {t.copied}
                              </>
                            ) : (
                              <>
                                <Copy className="h-3 w-3" />
                                {t.copyLink}
                              </>
                            )}
                          </button>
                        ) : (
                          <button
                            type="button"
                            onClick={() => rotate(l)}
                            disabled={revoking === l.id}
                            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-[11px] text-text-muted transition-colors hover:border-border-strong hover:text-text disabled:opacity-40"
                          >
                            <RefreshCw className="h-3 w-3" />
                            {t.rotate}
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => revoke(l)}
                          disabled={revoking === l.id}
                          className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-[11px] text-text-muted transition-colors hover:border-score-low/50 hover:text-score-low disabled:opacity-40"
                        >
                          <Trash2 className="h-3 w-3" />
                          {t.revoke}
                        </button>
                      </div>
                    </div>
                    <div className="flex flex-col gap-0.5 text-[10.5px] text-text-faint">
                      <code className="truncate font-mono">
                        {url ?? t.secretUnavailable}
                      </code>
                      <span>{t.expiresOn(new Date(l.expires_at).toLocaleDateString())}</span>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
