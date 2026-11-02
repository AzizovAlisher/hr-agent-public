// Branded fallback so a cold/slow API doesn't show a blank white page on the
// public apply link before the position data resolves.
export default function Loading() {
  return (
    <main className="min-h-screen bg-bg">
      <div className="mx-auto flex max-w-[640px] flex-col gap-7 px-5 py-10 sm:py-14">
        <div className="h-7 w-32 animate-pulse rounded-[6px] bg-bg-elevated" />
        <div className="flex flex-col gap-3 rounded-xl border border-border bg-bg-elevated p-6">
          <div className="h-3 w-20 animate-pulse rounded bg-bg" />
          <div className="h-6 w-3/4 animate-pulse rounded bg-bg" />
          <div className="h-4 w-full animate-pulse rounded bg-bg" />
          <div className="h-4 w-2/3 animate-pulse rounded bg-bg" />
        </div>
        <div className="h-48 animate-pulse rounded-xl border border-border bg-bg-elevated" />
      </div>
    </main>
  );
}
