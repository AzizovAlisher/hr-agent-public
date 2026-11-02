import { Skeleton } from "@/components/ui/skeleton";

export default function AppLoading() {
  return (
    <div className="flex min-h-full flex-col">
      <div className="sticky top-0 z-10 flex h-[53px] items-center justify-between gap-4 border-b border-border bg-background px-7">
        <Skeleton className="h-4 w-40" />
        <div className="flex items-center gap-2">
          <Skeleton className="h-7 w-32" />
          <Skeleton className="h-7 w-7 rounded-full" />
        </div>
      </div>
      <div className="flex-1 px-7 py-8">
        <div className="mx-auto flex max-w-[1200px] flex-col gap-6">
          <div className="flex items-end justify-between gap-4">
            <div className="flex flex-col gap-2">
              <Skeleton className="h-7 w-48" />
              <Skeleton className="h-4 w-72" />
            </div>
            <Skeleton className="h-9 w-32" />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <div
                key={i}
                className="flex flex-col gap-3 rounded-xl border border-border bg-bg-elevated p-5"
              >
                <Skeleton className="h-3.5 w-16" />
                <Skeleton className="h-5 w-3/4" />
                <Skeleton className="h-3 w-full" />
                <Skeleton className="h-3 w-5/6" />
                <div className="mt-2 flex items-center gap-2">
                  <Skeleton className="h-5 w-14" />
                  <Skeleton className="h-5 w-16" />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
