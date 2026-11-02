import type { ApiResponseSchema } from "@/lib/generated/contracts";

export type IntegrationProvider =
  ApiResponseSchema<"IntegrationProviderOut">["provider"];
export type IntegrationState =
  ApiResponseSchema<"IntegrationConnectionOut">["status"];
export type IntegrationProviderDefinition =
  ApiResponseSchema<"IntegrationProviderOut">;
export type IntegrationConnection =
  ApiResponseSchema<"IntegrationConnectionOut">;
export type IntegrationConnectionCreated =
  ApiResponseSchema<"IntegrationConnectionCreatedOut">;
export type IntegrationConnectionPage =
  ApiResponseSchema<"IntegrationConnectionPage">;
export type IntegrationJobStatus =
  ApiResponseSchema<"IntegrationJobOut">["status"];
export type IntegrationJob = ApiResponseSchema<"IntegrationJobOut">;
export type IntegrationJobPage = ApiResponseSchema<"IntegrationJobPage">;
export type IntegrationCredentials =
  ApiResponseSchema<"IntegrationCredentialsOut">;
export type IntegrationOAuthStart =
  ApiResponseSchema<"IntegrationOAuthStartOut">;
export type IntegrationHealth = ApiResponseSchema<"IntegrationHealthOut">;

/** @ui-only Permission-derived control state for the integration workspace. */
export type IntegrationAdminAccess = {
  read: boolean;
  manage: boolean;
  credentials: boolean;
};
