// Route-level Suspense fallback for the live page only — hence the (live) route group. A
// loading boundary flushes the shell (and a 200) before the page resolves, which would turn
// `notFound()` on /clips/[id] into a soft 404, so it is deliberately not placed at the app root.
//
// The nav, footer and page chrome paint immediately while the server is still waiting on
// Supabase (the live page is force-dynamic with a 4 s fetch timeout). Deliberately contains no
// numbers or text that could be mistaken for content — it is labelled as a load, not a stand-in
// for data.
function SkeletonPanel({ className = "" }: { className?: string }) {
  return (
    <div className={`panel skeleton-pulse ${className}`} aria-hidden="true">
      <div className="h-full w-full" />
    </div>
  );
}

export default function Loading() {
  return (
    <div className="flex flex-col gap-3 pt-3" role="status" aria-live="polite">
      <p className="ticker flex items-center gap-2 text-[0.62rem] text-smoke">
        <span className="led led-live" aria-hidden="true" />
        Loading live data…
      </p>
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex min-w-0 flex-col gap-3">
          <SkeletonPanel className="aspect-video w-full" />
          <SkeletonPanel className="h-56" />
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <SkeletonPanel className="h-40" />
          <SkeletonPanel className="h-72" />
        </div>
      </div>
    </div>
  );
}
