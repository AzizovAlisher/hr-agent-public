import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { LoginFlow } from "@/components/auth/login-flow";
import type { AuthFlowState } from "@/lib/auth-flow-types";
import { currentApiSession, currentMfaChallenge } from "@/lib/auth-session";
import { getMessages } from "@/lib/i18n";

/** @ui-only Login-page query state. */
type SearchParams = {
  error?: string;
  expired?: string;
  unavailable?: string;
  local_logout?: string;
};

export const metadata: Metadata = {
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const { error, expired, unavailable, local_logout: localLogout } =
    await searchParams;
  const [session, apiSession, challenge] = await Promise.all([
    auth(),
    currentApiSession(),
    currentMfaChallenge(),
  ]);
  if (session && apiSession && !expired && !unavailable) redirect("/my-work");
  const m = await getMessages();
  const initialState: AuthFlowState = challenge
    ? { step: challenge.purpose === "setup" ? "setup" : "verify" }
    : {
        step: "credentials",
        error: unavailable
          ? "service_unavailable"
          : expired
            ? "session_expired"
            : error
            ? "invalid_credentials"
            : undefined,
      };

  return (
    <main
      aria-label={m.login.title}
      className="relative flex min-h-screen flex-col items-center justify-center gap-6 px-4"
      style={{
        background: `
          radial-gradient(circle at 20% 0%, oklch(0.7 0.15 162 / 0.04) 0%, transparent 50%),
          radial-gradient(circle at 80% 100%, oklch(0.7 0.15 162 / 0.03) 0%, transparent 50%),
          var(--bg)
        `,
      }}
    >
      <div className="w-full max-w-[380px] rounded-xl border border-border bg-bg-elevated p-8 shadow-[0_16px_40px_-8px_rgb(0_0_0/0.6)]">
        {/* Brand */}
        <div className="mb-7 flex items-center gap-2.5">
          <span className="relative flex h-7 w-7 items-center justify-center rounded-[8px] bg-text font-bold text-bg text-[14px] tracking-tight">
            R
            <span className="absolute -right-[3px] -top-[3px] h-1.5 w-1.5 rounded-full bg-primary" />
          </span>
          <span className="text-[15px] font-semibold tracking-tight">
            {m.app.brandName}
          </span>
        </div>

        <LoginFlow initialState={initialState} />
        {localLogout ? (
          <p
            role="status"
            className="mt-4 rounded-md border border-warning/35 bg-warning/10 px-3 py-2 text-xs leading-5 text-text-muted"
          >
            {m.login.localLogoutWarning}
          </p>
        ) : null}
      </div>
      <div className="font-mono text-[11px] tracking-[0.04em] text-text-faint">
        {m.login.footer}
      </div>
    </main>
  );
}
