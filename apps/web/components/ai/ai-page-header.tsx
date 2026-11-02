import type { ReactNode } from "react";

export function AiPageHeader({
  eyebrow,
  title,
  subtitle,
  actions,
}: {
  eyebrow?: string;
  title: string;
  subtitle: string;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-col gap-4 border-b border-border px-5 py-5 sm:flex-row sm:items-start sm:justify-between sm:px-8">
      <div className="max-w-3xl">
        {eyebrow ? (
          <div className="mb-1.5 font-mono text-[10px] uppercase tracking-[0.1em] text-primary">
            {eyebrow}
          </div>
        ) : null}
        <h1 className="text-[23px] font-semibold tracking-[-0.025em]">{title}</h1>
        <p className="mt-1.5 max-w-2xl text-[13px] leading-5 text-text-muted">{subtitle}</p>
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}

