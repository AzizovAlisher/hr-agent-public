import type {
  ApiOperationRequestBody,
  ApiOperationResponse,
  ApiResponseSchema,
  ApiSchema,
} from "./contracts";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends
    (<Value>() => Value extends Right ? 1 : 2)
    ? true
    : false;
type Expect<Value extends true> = Value;

type LoginRequestUsesSchema = Expect<
  Equal<ApiOperationRequestBody<"/auth/login", "post">, ApiSchema<"LoginRequest">>
>;
type ApplicationRequestUsesSchema = Expect<
  Equal<
    ApiOperationRequestBody<"/applications", "post">,
    ApiSchema<"ApplicationCreate">
  >
>;
type ApplicationResponseUsesSchema = Expect<
  Equal<
    ApiOperationResponse<"/applications", "post">,
    ApiSchema<"ApplicationOut">
  >
>;
type SerializedApplicationHasRequiredDefaults = Expect<
  Equal<
    ApiResponseSchema<"ApplicationOut">["stage_events"],
    ApiResponseSchema<"ApplicationStageEventOut">[]
  >
>;

export type GeneratedContractAssertions =
  | LoginRequestUsesSchema
  | ApplicationRequestUsesSchema
  | ApplicationResponseUsesSchema
  | SerializedApplicationHasRequiredDefaults;
