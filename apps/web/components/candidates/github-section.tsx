"use client";

import { useEffect, useState, useTransition } from "react";
import { GitBranch, RefreshCw, Star, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  attachGithubAction,
  detachGithubAction,
  detectGithubAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";
import type { GitHubData } from "@/lib/types";

/** @ui-only GitHub section composition properties. */
type Props = {
  candidateId: string;
  login: string | null;
  data: GitHubData | null;
  fetchedAt: string | null;
  canManage: boolean;
  enrichmentAvailable: boolean;
};

/**
 * GitHub profile + repositories for a candidate. When nothing is attached we
 * auto-detect a github.com link in the CV (one-click attach) and also accept a
 * manually-pasted username/URL. Attaching fetches the profile, has the AI
 * absorb it, and feeds it into scoring / chat / interview prep.
 */
export function GitHubSection({
  candidateId,
  login,
  data,
  fetchedAt,
  canManage,
  enrichmentAvailable,
}: Props) {
  const { m } = useI18n();
  const t = m.enrichment.github;

  const [current, setCurrent] = useState<{
    login: string;
    data: GitHubData;
    fetchedAt: string | null;
  } | null>(login && data ? { login, data, fetchedAt } : null);
  const [input, setInput] = useState("");
  const [suggested, setSuggested] = useState<string | null>(null);
  const [busy, start] = useTransition();

  // Auto-detect a github link in the CV when nothing is attached yet.
  useEffect(() => {
    if (current || !enrichmentAvailable) return;
    let cancelled = false;
    void detectGithubAction(candidateId).then((res) => {
      if (!cancelled && res.ok) setSuggested(res.data.suggested_login);
    });
    return () => {
      cancelled = true;
    };
  }, [candidateId, current, enrichmentAvailable]);

  function attach(value: string) {
    const v = value.trim();
    if (!v) {
      toast.error(t.invalidToast);
      return;
    }
    start(async () => {
      const res = await attachGithubAction(candidateId, v);
      if (!res.ok) {
        toast.error(t.failedToast(res.error));
        return;
      }
      setCurrent({
        login: res.data.login,
        data: res.data.data,
        fetchedAt: res.data.fetched_at,
      });
      setInput("");
      setSuggested(null);
      toast.success(t.attachedToast);
    });
  }

  function detach() {
    if (!current) return;
    if (!window.confirm(t.detachConfirm)) return;
    const prev = current;
    setCurrent(null);
    void detachGithubAction(candidateId).then((res) => {
      if (!res.ok) {
        setCurrent(prev);
        toast.error(t.failedToast(res.error));
      }
    });
  }

  const count = current?.data.stats?.shown_repos ?? null;

  if (!current && (!enrichmentAvailable || !canManage)) return null;

  return (
    <div className="rounded-lg border border-border bg-bg-elevated">
      <header className="flex items-center justify-between gap-3 border-b border-border px-5 py-3.5">
        <div className="flex min-w-0 items-center gap-2">
          <GitBranch className="h-3.5 w-3.5 text-text-muted" />
          <h2 className="text-[13px] font-semibold tracking-tight">{t.title}</h2>
          {count != null ? (
            <span className="font-mono text-[10px] text-text-faint">· {count}</span>
          ) : null}
        </div>
        {current && canManage ? (
          <div className="flex items-center gap-1.5">
            {enrichmentAvailable ? (
              <button
                type="button"
                onClick={() => attach(current.login)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-2.5 py-1 text-[11px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover disabled:opacity-40"
              >
                <RefreshCw className={cn("h-3 w-3", busy && "animate-spin")} />
                {busy ? t.refreshing : t.refresh}
              </button>
            ) : null}
            <button
              type="button"
              onClick={detach}
              disabled={busy}
              className="inline-flex items-center gap-1 rounded-[6px] px-1.5 py-1 text-[11px] text-text-faint transition-colors hover:bg-score-low/10 hover:text-score-low disabled:opacity-40"
              aria-label={t.detach}
            >
              <Trash2 className="h-3 w-3" />
            </button>
          </div>
        ) : null}
      </header>

      {current ? (
        <Profile current={current} t={t} />
      ) : (
        <div className="flex flex-col gap-3 px-5 py-4">
          {suggested ? (
            <div className="flex items-center justify-between gap-3 rounded-md border border-accent-border bg-accent-muted/40 px-3 py-2">
              <span className="min-w-0 truncate text-[12.5px] text-text">
                {t.detectHint(suggested)}
              </span>
              <button
                type="button"
                onClick={() => attach(suggested)}
                disabled={busy}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-[6px] bg-text px-2.5 py-1 text-[11px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                {busy ? t.attaching : t.fetchDetected}
              </button>
            </div>
          ) : (
            <p className="text-[12.5px] text-text-muted">{t.empty}</p>
          )}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              attach(input);
            }}
            className="flex items-center gap-2"
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={t.placeholder}
              className="block w-full rounded-md border border-border bg-bg px-3 py-2 font-mono text-[12px] outline-none placeholder:text-text-faint focus:border-border-strong"
            />
            <button
              type="submit"
              disabled={busy || !input.trim()}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-text px-3 py-2 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {busy ? t.attaching : t.attach}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}

function Profile({
  current,
  t,
}: {
  current: { login: string; data: GitHubData; fetchedAt: string | null };
  t: ReturnType<typeof useI18n>["m"]["enrichment"]["github"];
}) {
  const { m } = useI18n();
  const p = current.data.profile;
  const stats = current.data.stats;
  const repos = current.data.repos ?? [];
  const languages = current.data.languages ?? [];

  return (
    <div className="flex flex-col gap-4 px-5 py-4">
      {/* Profile header */}
      <div className="flex items-start gap-3">
        {p.avatar_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={p.avatar_url}
            alt={p.login ?? "avatar"}
            className="h-11 w-11 shrink-0 rounded-full border border-border"
          />
        ) : (
          <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full border border-border bg-bg text-text-muted">
            <GitBranch className="h-5 w-5" />
          </span>
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <a
              href={p.html_url ?? `https://github.com/${current.login}`}
              target="_blank"
              rel="noreferrer"
              className="truncate text-[14px] font-semibold text-text transition-colors hover:text-primary"
            >
              {p.name || p.login || current.login}
            </a>
            <span className="font-mono text-[11px] text-text-faint">
              @{p.login ?? current.login}
            </span>
          </div>
          {p.bio ? (
            <p className="mt-0.5 line-clamp-2 text-[12px] text-text-muted">{p.bio}</p>
          ) : null}
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-[10.5px] text-text-faint">
            <span>
              {p.public_repos} {t.publicRepos}
            </span>
            <span>
              {p.followers} {t.followers}
            </span>
            {stats?.total_stars ? (
              <span className="inline-flex items-center gap-0.5">
                <Star className="h-2.5 w-2.5" /> {stats.total_stars} {t.totalStars}
              </span>
            ) : null}
            {current.fetchedAt ? (
              <span>{t.fetchedMeta(m.format.relativeTime(current.fetchedAt))}</span>
            ) : null}
          </div>
        </div>
      </div>

      {/* AI summary */}
      {current.data.ai_summary ? (
        <div className="rounded-md border border-border bg-bg px-3.5 py-2.5">
          <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {t.aiSummary}
          </div>
          <p className="text-[12.5px] leading-[1.55] text-text">
            {current.data.ai_summary}
          </p>
        </div>
      ) : null}

      {/* Languages */}
      {languages.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {languages.slice(0, 8).map((l) => (
            <span
              key={l.language}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-bg px-2 py-0.5 text-[10.5px] text-text-muted"
            >
              {l.language}
              <span className="font-mono text-text-faint">{l.repo_count}</span>
            </span>
          ))}
        </div>
      ) : null}

      {/* Top repos */}
      <div>
        <div className="mb-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {t.topRepos}
        </div>
        {repos.length === 0 ? (
          <p className="text-[12px] text-text-muted">{t.noRepos}</p>
        ) : (
          <ul className="flex flex-col divide-y divide-border rounded-md border border-border">
            {repos.map((r) => (
              <li key={r.full_name ?? r.name} className="flex flex-col gap-0.5 px-3 py-2">
                <div className="flex items-center gap-2">
                  <a
                    href={r.html_url ?? "#"}
                    target="_blank"
                    rel="noreferrer"
                    className="truncate text-[12.5px] font-medium text-text transition-colors hover:text-primary"
                  >
                    {r.name}
                  </a>
                  {r.language ? (
                    <span className="shrink-0 font-mono text-[10px] text-text-faint">
                      {r.language}
                    </span>
                  ) : null}
                  {r.stars > 0 ? (
                    <span className="inline-flex shrink-0 items-center gap-0.5 font-mono text-[10px] text-text-faint">
                      <Star className="h-2.5 w-2.5" /> {r.stars}
                    </span>
                  ) : null}
                </div>
                {r.description ? (
                  <p className="line-clamp-1 text-[11.5px] text-text-muted">
                    {r.description}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
