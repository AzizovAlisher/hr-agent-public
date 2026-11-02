import { notFound } from "next/navigation";
import { ShieldCheck } from "lucide-react";

import { getPublicPosition } from "@/lib/public-actions";
import { getLocale, getMessages } from "@/lib/i18n";
import { ApplyForm } from "@/components/public/apply-form";
import { ApplyLocaleSwitch } from "@/components/public/apply-locale-switch";
import { BrandMark } from "@/components/public/brand-mark";
import { RequirementChips } from "@/components/positions/requirement-chips";
import { publicBrandingUrl } from "@/lib/public-branding";

export const dynamic = "force-dynamic";

/** @ui-only Next.js public-application route properties. */
type Props = {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ utm_source?: string; ref?: string }>;
};

export default async function PublicApplyPage({ params, searchParams }: Props) {
  const { id } = await params;
  const { utm_source: utmSource, ref: referralToken } = await searchParams;

  const position = await getPublicPosition(id);
  if (!position) notFound();

  const locale = await getLocale();
  const m = await getMessages();
  const t = m.apply;

  const desc = (position.description ?? "").trim();
  const fullDesc = (position.full_description ?? "").trim();
  // Brand per-vacancy: prefer the hiring company on the role (requirements.company,
  // cleaned of trailing "— department" / "(descriptor)"), fall back to the org.
  // On a confidential role the API has already withheld every one of these —
  // company is stripped from requirements and organization_name is null — so
  // this resolves to the localized "confidential employer" label instead.
  const companyRaw =
    typeof position.requirements?.company === "string"
      ? (position.requirements.company as string)
      : "";
  const brandName =
    companyRaw.split("—")[0].split("(")[0].trim() ||
    position.organization_name ||
    t.confidentialEmployer;
  const accent = position.organization_accent_color;
  const logoUrl = publicBrandingUrl(position.organization_logo_path);
  // Override --primary on the public page so the buyer's brand color tints
  // CTAs without us having to thread accentColor into every component.
  const styleOverride = accent
    ? ({ "--primary": accent } as React.CSSProperties)
    : undefined;

  return (
    <main className="min-h-screen bg-bg" style={styleOverride}>
      <div className="mx-auto flex max-w-[640px] flex-col gap-7 px-5 py-10 sm:py-14">
        {/* Brand strip */}
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <BrandMark
              logoUrl={logoUrl}
              accentColor={accent}
              orgName={brandName}
              confidential={position.employer_confidential}
            />
            {logoUrl ? null : (
              <span className="text-[12.5px] font-medium tracking-tight text-text-muted">
                {brandName}
              </span>
            )}
          </div>
          <ApplyLocaleSwitch />
        </div>

        {/* Position card */}
        <header className="flex flex-col gap-3 rounded-xl border border-border bg-bg-elevated p-6">
          <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
            {t.positionTag}
          </span>
          <h1 className="text-[22px] font-semibold leading-tight tracking-[-0.01em]">
            {position.title}
          </h1>
          {desc ? (
            <p className="text-[13.5px] leading-[1.6] text-text-muted">
              {desc}
            </p>
          ) : null}
          {position.employer_confidential ? (
            <p className="flex items-start gap-2 rounded-lg border border-border bg-bg px-3 py-2 text-[12px] leading-[1.55] text-text-faint">
              <ShieldCheck className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>{t.confidentialNote}</span>
            </p>
          ) : null}
          <RequirementChips
            requirements={position.requirements}
            locale={locale}
            size="md"
            max={8}
            className="mt-1"
          />
          {fullDesc ? (
            <details className="group mt-2 rounded-lg border border-border bg-bg">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-4 py-2.5 text-[12px] font-medium text-text-muted transition-colors hover:text-text">
                <span>{t.fullDescriptionLabel}</span>
                <span className="font-mono text-[10.5px] text-text-faint transition-transform group-open:rotate-90">
                  ›
                </span>
              </summary>
              <div className="border-t border-border px-4 py-3 text-[12.5px] leading-[1.65] text-text-muted whitespace-pre-line">
                {fullDesc}
              </div>
            </details>
          ) : null}
        </header>

        {/* Apply form */}
        <section className="flex flex-col gap-4 rounded-xl border border-border bg-bg-elevated p-6">
          <div>
            <h2 className="text-[15px] font-semibold tracking-tight">
              {t.formTitle}
            </h2>
            <p className="mt-1 text-[12.5px] text-text-muted">{t.formIntro}</p>
          </div>
          <ApplyForm
            positionId={position.id}
            controllerName={brandName}
            utmSource={utmSource ?? null}
            referralToken={referralToken ?? null}
          />
        </section>

        <footer className="text-center font-mono text-[10px] tracking-[0.04em] text-text-faint">
          {t.brandFooter}
        </footer>
      </div>
    </main>
  );
}
