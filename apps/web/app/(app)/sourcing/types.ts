import type { ApiResponseSchema } from "@/lib/generated/contracts";

/** OpenAPI-backed compatibility names used by the sourcing workspace. */
export type SourceProvider = ApiResponseSchema<"SourceIntegrationOut">["provider"];
export type SourceCapabilityState =
  ApiResponseSchema<"SourceCapabilitiesOut">["candidate_import"];
export type SourceCapabilities = ApiResponseSchema<"SourceCapabilitiesOut">;
export type SourceIntegration = ApiResponseSchema<"SourceIntegrationOut">;
export type SourceIntegrationPage = ApiResponseSchema<"SourceIntegrationPage">;
export type SourceIntegrationCreated = ApiResponseSchema<"SourceIntegrationCreatedOut">;
export type SourceImportRun = ApiResponseSchema<"SourceImportRunOut">;
export type SourceImportRunPage = ApiResponseSchema<"SourceImportRunPage">;
export type SourceJob = ApiResponseSchema<"SourceJobOut">;
export type SourceImportFilePreview =
  ApiResponseSchema<"SourceImportFilePreviewOut">;
export type SourceImportOutcome = SourceImportRun | SourceJob;
export type ReferralLink = ApiResponseSchema<"ReferralLinkOut">;
export type ReferralLinkCreated = ApiResponseSchema<"ReferralLinkCreatedOut">;
export type SourcePublication = ApiResponseSchema<"SourcePublicationOut">;
export type SourceAnalytics = ApiResponseSchema<"SourceAnalyticsOut">;
export type SourceWebhookSecret =
  ApiResponseSchema<"SourceWebhookSecretOut">;
export type SourcingPosition = Pick<
  ApiResponseSchema<"PositionOut">,
  "id" | "title" | "status"
>;

/** @ui-only Load state; it is not sent to or received from the API. */
export type SourcingLoadFailures = Record<
  | "integrations"
  | "runs"
  | "positions"
  | "analytics"
  | "referrals"
  | "providerConnections",
  boolean
>;
