/** @ui-only Stable login-form error state. */
export type AuthFlowError =
  | "invalid_credentials"
  | "session_expired"
  | "invalid_code"
  | "challenge_expired"
  | "setup_already_started"
  | "rate_limited"
  | "service_unavailable"
  | "invalid_request";

/** @ui-only Client login/MFA state machine. */
export type AuthFlowState = {
  step: "credentials" | "setup" | "verify" | "recovery";
  error?: AuthFlowError;
  retryAfter?: number;
  setup?: { secret: string; provisioningUri: string };
  recoveryCodes?: string[];
};

export const INITIAL_AUTH_FLOW_STATE: AuthFlowState = { step: "credentials" };
