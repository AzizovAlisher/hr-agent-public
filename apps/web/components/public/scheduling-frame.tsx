import { ApplyLocaleSwitch } from "@/components/public/apply-locale-switch";
import { BrandMark } from "@/components/public/brand-mark";


export function SchedulingFrame({
  organizationName,
  logoUrl,
  accentColor,
  children,
}: {
  organizationName: string;
  logoUrl: string | null;
  accentColor: string | null;
  children: React.ReactNode;
}) {
  const style = accentColor
    ? ({ "--primary": accentColor } as React.CSSProperties)
    : undefined;
  return (
    <main className="min-h-screen bg-bg" style={style}>
      <div className="mx-auto flex w-full max-w-[760px] flex-col gap-6 px-5 py-10 sm:py-14">
        <div className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <BrandMark
              logoUrl={logoUrl}
              accentColor={accentColor}
              orgName={organizationName}
            />
            <span className="truncate text-[12.5px] font-medium tracking-tight text-text-muted">
              {organizationName}
            </span>
          </div>
          <ApplyLocaleSwitch />
        </div>
        {children}
      </div>
    </main>
  );
}
