import { FileX2 } from "lucide-react";

import { PublicOfferResponse } from "@/components/offers/public-offer-response";
import { SchedulingFrame } from "@/components/public/scheduling-frame";
import { getMessages } from "@/lib/i18n";
import { getPublicOffer } from "@/lib/public-offers";

export const dynamic = "force-dynamic";

export default async function PublicOfferPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const [{ token }, m] = await Promise.all([params, getMessages()]);
  const result = await getPublicOffer(token);

  if (!result.ok) {
    return (
      <SchedulingFrame
        organizationName={m.app.brandName}
        logoUrl={null}
        accentColor={null}
      >
        <section className="rounded-xl border border-border bg-bg-elevated px-6 py-14 text-center">
          <FileX2 className="mx-auto h-9 w-9 text-text-faint" />
          <h1 className="mt-4 text-[18px] font-semibold">
            {m.offers.public.notFoundTitle}
          </h1>
          <p className="mx-auto mt-2 max-w-md text-[12.5px] leading-5 text-text-muted">
            {m.offers.public.notFoundDesc}
          </p>
        </section>
      </SchedulingFrame>
    );
  }

  return (
    <SchedulingFrame
      organizationName={result.data.organization_name}
      logoUrl={null}
      accentColor={null}
    >
      <PublicOfferResponse token={token} initialOffer={result.data} />
    </SchedulingFrame>
  );
}
