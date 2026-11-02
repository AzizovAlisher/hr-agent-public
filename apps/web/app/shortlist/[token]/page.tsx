import { notFound } from "next/navigation";
import { Mail, Phone, Sparkles, User as UserIcon } from "lucide-react";

import { getMessages } from "@/lib/i18n";
import { getPublicShortlist } from "@/lib/public-shortlist";
import { SCORE_VAR, scoreTier } from "@/lib/score";
import type { PublicShortlistCandidate } from "@/lib/types";
import { ApplyLocaleSwitch } from "@/components/public/apply-locale-switch";
import { BrandMark } from "@/components/public/brand-mark";
import { SourceBadge } from "@/components/ui/source-badge";
import { publicBrandingUrl } from "@/lib/public-branding";

export const dynamic = "force-dynamic";

/** @ui-only Next.js public-shortlist route properties. */
type Props = {
  params: Promise<{ token: string }>;
};

export default async function PublicShortlistPage({ params }: Props) {
  const { token } = await params;
  const m = await getMessages();
  const t = m.shortlist;
  const candidate = m.candidate;

  const data = await getPublicShortlist(token);
  if (!data) notFound();

  const issuedRel = m.format.relativeTime(data.issued_at);
  // Brand per-vacancy: prefer the role's hiring company, fall back to the org.
  const brandName =
    (data.position_company ?? "").split("—")[0].split("(")[0].trim() ||
    data.organization_name ||
    m.apply.confidentialEmployer;
  const accent = data.organization_accent_color;
  const logoUrl = publicBrandingUrl(data.organization_logo_path);
  const styleOverride = accent
    ? ({ "--primary": accent } as React.CSSProperties)
    : undefined;

  return (
    <main className="min-h-screen bg-bg" style={styleOverride}>
      <div className="mx-auto flex w-full max-w-[920px] flex-col gap-7 px-5 py-10 sm:py-14">
        {/* Brand strip */}
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <BrandMark
              logoUrl={logoUrl}
              accentColor={accent}
              orgName={brandName}
              confidential={data.employer_confidential}
            />
            <span className="text-[12.5px] font-medium tracking-tight text-text-muted">
              {t.publicSharedBy(brandName)}
            </span>
          </div>
          <ApplyLocaleSwitch />
        </div>

        {/* Position header */}
        <header className="flex flex-col gap-3 rounded-xl border border-border bg-bg-elevated p-6">
          <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
            {t.publicTitle}
          </span>
          <h1 className="text-[22px] font-semibold leading-tight tracking-[-0.01em]">
            {data.position_title}
          </h1>
          <p className="font-mono text-[11.5px] text-text-faint">
            {t.publicSubtitle(data.candidates.length)} · {issuedRel}
          </p>
          {data.position_description ? (
            <p className="text-[13px] leading-[1.6] text-text-muted">
              {data.position_description}
            </p>
          ) : null}
        </header>

        {/* Candidates */}
        {data.candidates.length === 0 ? (
          <div className="rounded-xl border border-border bg-bg-elevated px-6 py-10 text-center">
            <p className="text-[13px] text-text-muted">
              {t.publicCandidatesEmpty}
            </p>
          </div>
        ) : (
          <ol className="flex flex-col gap-3">
            {data.candidates.map((c, idx) => {
              const contactCandidate = "candidate_id" in c ? c : null;
              return (
                <CandidateCard
                  key={contactCandidate?.candidate_id ?? `anonymous-${idx}`}
                  rank={idx + 1}
                  candidate={c}
                  criteria={[
                    {
                      label: candidate.criteria.technical,
                      score: c.score_technical_skills,
                      comment:
                        contactCandidate?.comment_technical_skills ?? null,
                    },
                    {
                      label: candidate.criteria.projects,
                      score: c.score_quality_of_projects,
                      comment:
                        contactCandidate?.comment_quality_of_projects ?? null,
                    },
                    {
                      label: candidate.criteria.experience,
                      score: c.score_previous_experience,
                      comment:
                        contactCandidate?.comment_previous_experience ?? null,
                    },
                    {
                      label: candidate.criteria.impact,
                      score: c.score_impact,
                      comment: contactCandidate?.comment_impact ?? null,
                    },
                  ]}
                  t={t}
                  contactLabel={t.publicContact}
                  showContact={data.include_contact}
                  showEvidence={contactCandidate !== null}
                />
              );
            })}
          </ol>
        )}

        <div className="flex items-center justify-center gap-2 rounded-lg border border-border bg-bg-elevated px-4 py-3 text-[11px] text-text-faint">
          <Sparkles className="h-3 w-3" />
          {t.publicFooterNote}
        </div>

        <footer className="text-center font-mono text-[10px] tracking-[0.04em] text-text-faint">
          {m.apply.brandFooter}
        </footer>
      </div>
    </main>
  );
}

function CandidateCard({
  rank,
  candidate,
  criteria,
  t,
  contactLabel,
  showContact,
  showEvidence,
}: {
  rank: number;
  candidate: PublicShortlistCandidate;
  criteria: { label: string; score: number | null; comment: string | null }[];
  t: {
    publicNoComment: string;
    publicNotScored: string;
  };
  contactLabel: string;
  showContact: boolean;
  showEvidence: boolean;
}) {
  const tier = scoreTier(candidate.overall_score);
  const overallColor = tier ? SCORE_VAR[tier] : "var(--text-faint)";
  const contactCandidate = "candidate_id" in candidate ? candidate : null;

  return (
    <li className="flex flex-col gap-4 rounded-xl border border-border bg-bg-elevated p-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[11px] text-text-faint tabular-nums">
              #{rank.toString().padStart(2, "0")}
            </span>
            {contactCandidate ? (
              <SourceBadge source={contactCandidate.source} />
            ) : null}
          </div>
          <h2 className="truncate text-[16px] font-semibold tracking-tight">
            {candidate.label}
          </h2>
          {showContact && contactCandidate ? (
            <ContactRow
              name={contactCandidate.contact_name}
              phone={contactCandidate.contact_phone}
              email={contactCandidate.contact_email}
              label={contactLabel}
            />
          ) : null}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span
            className="font-mono text-[26px] font-semibold leading-none tabular-nums"
            style={{ color: overallColor }}
          >
            {candidate.overall_score == null
              ? "—"
              : candidate.overall_score.toFixed(1)}
          </span>
          <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
            overall
          </span>
        </div>
      </div>

      {/* Rubric grid */}
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        {criteria.map((cr) => {
          const ct = scoreTier(cr.score);
          const color = ct ? SCORE_VAR[ct] : "var(--text-faint)";
          const pct =
            cr.score == null
              ? 0
              : Math.max(0, Math.min(100, cr.score)) / 100;
          return (
            <div
              key={cr.label}
              className="flex flex-col gap-1.5 rounded-lg border border-border bg-bg p-3.5"
            >
              <div className="flex items-baseline justify-between">
                <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-muted">
                  {cr.label}
                </span>
                <span
                  className="font-mono text-[14px] font-semibold tabular-nums"
                  style={{ color }}
                >
                  {cr.score == null ? "—" : cr.score.toFixed(1)}
                </span>
              </div>
              <div className="h-1 w-full overflow-hidden rounded-[2px] bg-border">
                <div
                  className="bar-fill h-full rounded-[2px]"
                  style={{ width: `${pct * 100}%`, background: color }}
                />
              </div>
              {showEvidence ? (
                <p className="line-clamp-4 text-[12px] leading-[1.55] text-text-muted">
                  {cr.comment && cr.comment.trim().length > 0
                    ? cr.comment
                    : cr.score == null
                      ? t.publicNotScored
                      : t.publicNoComment}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
    </li>
  );
}

function ContactRow({
  name,
  phone,
  email,
  label,
}: {
  name: string | null;
  phone: string | null;
  email: string | null;
  label: string;
}) {
  if (!name && !phone && !email) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-text-muted">
      <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
        {label}
      </span>
      {name ? (
        <span className="inline-flex items-center gap-1">
          <UserIcon className="h-3 w-3 text-text-faint" />
          {name}
        </span>
      ) : null}
      {phone ? (
        <a
          href={`tel:${phone}`}
          className="inline-flex items-center gap-1 transition-colors hover:text-text"
        >
          <Phone className="h-3 w-3 text-text-faint" />
          {phone}
        </a>
      ) : null}
      {email ? (
        <a
          href={`mailto:${email}`}
          className="inline-flex items-center gap-1 transition-colors hover:text-text"
        >
          <Mail className="h-3 w-3 text-text-faint" />
          {email}
        </a>
      ) : null}
    </div>
  );
}
