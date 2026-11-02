import { ApplyLocaleSwitch } from "@/components/public/apply-locale-switch";

export function AccessShell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-bg px-4 py-10">
      <section
        aria-labelledby="access-page-title"
        className="w-full max-w-[460px] rounded-2xl border border-border bg-bg-elevated p-6 shadow-xl sm:p-8"
      >
        <header className="mb-7">
          <div className="mb-6 flex items-center justify-between gap-3">
            <span className="flex items-center gap-2.5 text-[15px] font-semibold tracking-tight">
              <span className="relative flex h-7 w-7 items-center justify-center rounded-[8px] bg-text text-[14px] font-bold text-bg">
                R
                <span className="absolute -right-[3px] -top-[3px] h-1.5 w-1.5 rounded-full bg-primary" />
              </span>
              Recruiter
            </span>
            <ApplyLocaleSwitch />
          </div>
          <h1 id="access-page-title" className="text-2xl font-semibold tracking-tight">
            {title}
          </h1>
          <p className="mt-2 text-sm leading-6 text-text-muted">{subtitle}</p>
        </header>
        {children}
      </section>
    </main>
  );
}
