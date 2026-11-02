import { Suspense } from "react";

import { OAuthCallbackCompleter } from "@/components/integrations/oauth-callback-completer";

export const dynamic = "force-dynamic";

export default function IntegrationOAuthCallbackPage() {
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <Suspense fallback={<p className="text-sm text-text-muted">Completing OAuth…</p>}>
        <OAuthCallbackCompleter />
      </Suspense>
    </div>
  );
}
