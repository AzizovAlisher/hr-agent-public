export default function AssessmentLoading() {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-background px-4">
      <div role="status" className="flex items-center gap-3 text-sm text-text-muted">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-primary motion-reduce:animate-none" />
        Loading assessment…
      </div>
    </main>
  );
}
