import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, ChevronLeft } from "lucide-react";

import { apiGet } from "@/lib/api-client";
import { candRef } from "@/lib/format";
import { getMessages } from "@/lib/i18n";
import { SCORE_VAR, scoreTier } from "@/lib/score";
import type {
  PositionDetail,
  RankedCandidate,
  RankedCandidatePage,
} from "@/lib/types";
import { ScoreChip } from "@/components/ui/score-chip";
import { SourceBadge } from "@/components/ui/source-badge";
import { Topbar } from "@/components/app/topbar";

export const dynamic = "force-dynamic";

const MIN = 2;
const MAX = 4;

/** @ui-only Next.js position-comparison route properties. */
type Props = {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ ids?: string }>;
};

/** @ui-only Score fields displayed by the comparison table. */
type CriterionKey =
  | "score_technical_skills"
  | "score_quality_of_projects"
  | "score_previous_experience"
  | "score_impact";

/** @ui-only Comment fields displayed by the comparison table. */
type CommentKey =
  | "comment_technical_skills"
  | "comment_quality_of_projects"
  | "comment_previous_experience"
  | "comment_impact";

export default async function ComparePage({ params, searchParams }: Props) {
  const { id } = await params;
  const { ids: idsParam } = await searchParams;
  const m = await getMessages();
  const t = m.compare;

  const idsRaw = (idsParam ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const ids = Array.from(new Set(idsRaw)).slice(0, MAX);

  let position: PositionDetail | null = null;
  let pipeline: RankedCandidatePage | null = null;
  try {
    const query = new URLSearchParams({ limit: String(MAX) });
    for (const candidateId of ids) query.append("candidate_id", candidateId);
    [position, pipeline] = await Promise.all([
      apiGet<PositionDetail>(`/positions/${id}`),
      apiGet<RankedCandidatePage>(
        `/positions/${id}/applications?${query.toString()}`,
      ),
    ]);
  } catch {
    notFound();
  }
  if (!position || !pipeline) notFound();

  const byId = new Map(pipeline.items.map((c) => [c.candidate_id, c]));
  const candidates: RankedCandidate[] = ids
    .map((cid) => byId.get(cid))
    .filter((c): c is RankedCandidate => c != null);
  const droppedCount = ids.length - candidates.length;

  if (candidates.length < MIN) {
    return (
      <>
        <Topbar
          crumbs={[
            { label: m.position.crumb, href: "/dashboard" },
            { label: position.title, href: `/positions/${position.id}` },
            { label: t.crumb },
          ]}
        />
        <div className="flex flex-1 items-center justify-center p-10">
          <div className="flex max-w-md flex-col items-center gap-3 rounded-xl border border-border bg-bg-elevated px-6 py-10 text-center">
            <h2 className="text-[15px] font-semibold tracking-tight">
              {t.selectMore}
            </h2>
            <p className="text-[12.5px] leading-[1.6] text-text-muted">
              {t.selectHint}
            </p>
            <Link
              href={`/positions/${position.id}`}
              className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[12.5px] text-text-muted transition-colors hover:border-border-strong hover:text-text"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              {t.backToPosition}
            </Link>
          </div>
        </div>
      </>
    );
  }

  const criteria: { score: CriterionKey; comment: CommentKey; label: string }[] =
    [
      {
        score: "score_technical_skills",
        comment: "comment_technical_skills",
        label: m.candidate.criteria.technical,
      },
      {
        score: "score_quality_of_projects",
        comment: "comment_quality_of_projects",
        label: m.candidate.criteria.projects,
      },
      {
        score: "score_previous_experience",
        comment: "comment_previous_experience",
        label: m.candidate.criteria.experience,
      },
      {
        score: "score_impact",
        comment: "comment_impact",
        label: m.candidate.criteria.impact,
      },
    ];

  return (
    <>
      <Topbar
        crumbs={[
          { label: m.position.crumb, href: "/dashboard" },
          { label: position.title, href: `/positions/${position.id}` },
          { label: t.crumb },
        ]}
      />

      <div className="flex-1 overflow-y-auto">
        <div className="px-7 pb-16 pt-7">
          <div className="mb-6 flex items-end justify-between gap-4">
            <div className="flex flex-col gap-1.5">
              <Link
                href={`/positions/${position.id}`}
                className="inline-flex w-fit items-center gap-1.5 text-[11.5px] text-text-muted transition-colors hover:text-text"
              >
                <ArrowLeft className="h-3 w-3" />
                {position.title}
              </Link>
              <h1 className="text-[20px] font-semibold leading-tight tracking-[-0.01em]">
                {t.title}
              </h1>
              <p className="font-mono text-[11.5px] text-text-faint">
                {t.subtitle(candidates.length)}
              </p>
            </div>
          </div>

          {droppedCount > 0 ? (
            <div className="mb-4 rounded-md border border-score-mid/30 bg-score-mid/10 px-3 py-2 text-[12px] text-score-mid">
              {t.droppedNotice(droppedCount)}
            </div>
          ) : null}

          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-separate border-spacing-0 text-left">
              <colgroup>
                <col className="w-[140px]" />
                {candidates.map((c) => (
                  <col key={c.candidate_id} />
                ))}
              </colgroup>

              <thead>
                <tr>
                  <th className="sticky left-0 z-10 border-b border-border bg-bg p-3 align-bottom text-[10px] uppercase tracking-[0.08em] text-text-faint" />
                  {candidates.map((c) => {
                    const ref =
                      c.display_name?.trim() ||
                      candRef(c.candidate_id, c.source_ref);
                    return (
                      <th
                        key={c.candidate_id}
                        className="border-b border-l border-border bg-bg-elevated p-3 align-bottom"
                      >
                        <div className="flex flex-col gap-2">
                          <SourceBadge source={c.source} />
                          <Link
                            href={`/candidates/${c.candidate_id}`}
                            className="truncate font-mono text-[12.5px] font-semibold text-text transition-colors hover:underline"
                            title={ref}
                          >
                            {ref}
                          </Link>
                          <div className="mt-1 flex items-center gap-2">
                            <ScoreChip score={c.overall_score} />
                            <span className="text-[10.5px] uppercase tracking-[0.08em] text-text-faint">
                              {t.overall}
                            </span>
                          </div>
                        </div>
                      </th>
                    );
                  })}
                </tr>
              </thead>

              <tbody>
                {criteria.map((cr, rowIdx) => (
                  <tr key={cr.score}>
                    <th
                      className="sticky left-0 z-10 border-b border-border bg-bg p-3 align-top text-left"
                      style={{ verticalAlign: "top" }}
                    >
                      <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-text-muted">
                        {cr.label}
                      </span>
                    </th>
                    {candidates.map((c) => {
                      const score = c[cr.score] as number | null;
                      const comment = c[cr.comment] as string | null;
                      return (
                        <td
                          key={c.candidate_id + cr.score}
                          className="border-b border-l border-border bg-bg-elevated p-4 align-top"
                        >
                          <div className="flex flex-col gap-2">
                            <ScoreCell score={score} />
                            <p className="text-[12.5px] leading-[1.6] text-text-muted">
                              {comment && comment.trim().length > 0
                                ? comment
                                : score == null
                                  ? t.notScored
                                  : t.noComment}
                            </p>
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  );
}

function ScoreCell({ score }: { score: number | null }) {
  const tier = scoreTier(score);
  const color = tier ? SCORE_VAR[tier] : "var(--text-faint)";
  const pct = score == null ? 0 : Math.max(0, Math.min(100, score)) / 100;
  return (
    <div className="flex flex-col gap-1.5">
      <span
        className="font-mono text-[15px] font-semibold tabular-nums"
        style={{ color }}
      >
        {score == null ? "—" : Math.round(score)}
      </span>
      <div className="h-1 w-full overflow-hidden rounded-[2px] bg-border">
        <div
          className="bar-fill h-full rounded-[2px]"
          style={{ width: `${pct * 100}%`, background: color }}
        />
      </div>
    </div>
  );
}
