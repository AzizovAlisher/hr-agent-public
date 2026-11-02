import { getMessages } from "@/lib/i18n";
import { ApplyLocaleSwitch } from "@/components/public/apply-locale-switch";

export default async function ApplyNotFound() {
  const m = await getMessages();
  const t = m.errors;
  const apply = m.apply;

  return (
    <main className="min-h-screen bg-bg">
      <div className="mx-auto flex max-w-[640px] flex-col gap-7 px-5 py-10 sm:py-14">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="relative flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-md bg-text font-bold text-bg text-[11px] tracking-tight">
              R
              <span className="absolute -right-[3px] -top-[3px] h-1.5 w-1.5 rounded-full bg-primary" />
            </span>
          </div>
          <ApplyLocaleSwitch />
        </div>

        <div className="flex flex-col gap-3 rounded-xl border border-border bg-bg-elevated p-8 text-center">
          <h1 className="text-[18px] font-semibold tracking-tight">
            {t.applyClosedTitle}
          </h1>
          <p className="mx-auto max-w-md text-[13px] leading-[1.6] text-text-muted">
            {t.applyClosedDesc}
          </p>
        </div>

        <footer className="text-center font-mono text-[10px] tracking-[0.04em] text-text-faint">
          {apply.brandFooter}
        </footer>
      </div>
    </main>
  );
}
