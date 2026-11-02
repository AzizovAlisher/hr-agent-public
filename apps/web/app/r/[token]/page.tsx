import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";

import { resolvePublicReferral } from "@/lib/public-actions";

export const dynamic = "force-dynamic";
export const metadata: Metadata = {
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default async function ReferralRedirectPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const referral = await resolvePublicReferral(token);
  if (!referral) notFound();
  redirect(referral.apply_url);
}
