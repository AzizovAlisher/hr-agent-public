/**
 * Stable type helpers over the generated OpenAPI document.
 *
 * `api.ts` is regenerated from FastAPI and must never be edited by hand. App
 * code should import schemas through this module (or through `lib/types.ts`'s
 * compatibility aliases) so request and response shapes have one source of
 * truth.
 */
import type { components, operations, paths } from "./api";

export type ApiComponents = components;
export type ApiOperations = operations;
export type ApiPaths = paths;

export type ApiSchema<Name extends keyof components["schemas"]> =
  components["schemas"][Name];

/**
 * FastAPI serializes response-model defaults and nullable fields instead of
 * omitting them. openapi-typescript represents defaulted properties as optional,
 * so this helper models the fully serialized response consumed by the existing
 * UI without re-declaring any field names or value types.
 */
export type ApiSerialized<Value> = Value extends readonly (infer Item)[]
  ? ApiSerialized<Item>[]
  : Value extends object
    ? { [Key in keyof Value]-?: ApiSerialized<Exclude<Value[Key], undefined>> }
    : Exclude<Value, undefined>;

export type ApiResponseSchema<Name extends keyof components["schemas"]> =
  ApiSerialized<ApiSchema<Name>>;

export type ApiPath = keyof paths;
export type ApiMethod = "get" | "put" | "post" | "delete" | "patch";

export type ApiOperation<
  Path extends ApiPath,
  Method extends ApiMethod,
> = paths[Path][Method] extends object ? paths[Path][Method] : never;

export type ApiOperationParameters<
  Path extends ApiPath,
  Method extends ApiMethod,
> = ApiOperation<Path, Method> extends { parameters: infer Parameters }
  ? Parameters
  : never;

export type ApiOperationQuery<
  Path extends ApiPath,
  Method extends ApiMethod,
> = ApiOperationParameters<Path, Method> extends { query?: infer Query }
  ? Query
  : never;

export type ApiOperationPathParameters<
  Path extends ApiPath,
  Method extends ApiMethod,
> = ApiOperationParameters<Path, Method> extends { path?: infer Parameters }
  ? Parameters
  : never;

type RequestContent<Operation> = Operation extends {
  requestBody: { content: infer Content };
}
  ? Content
  : Operation extends { requestBody?: { content: infer Content } }
    ? Content
    : never;

export type ApiOperationRequestBody<
  Path extends ApiPath,
  Method extends ApiMethod,
  MediaType extends string = "application/json",
> = RequestContent<ApiOperation<Path, Method>> extends infer Content
  ? MediaType extends keyof Content
    ? Content[MediaType]
    : never
  : never;

type SuccessStatus = 200 | 201 | 202 | 204;

type ResponseContent<Response> = Response extends { content: infer Content }
  ? Content extends { "application/json": infer Json }
    ? Json
    : Content extends { "application/problem+json": infer Problem }
      ? Problem
      : Content extends { "text/event-stream": infer EventStream }
        ? EventStream
        : unknown
  : void;

type OperationResponses<Operation> = Operation extends {
  responses: infer Responses;
}
  ? Responses
  : never;

export type ApiOperationResponse<
  Path extends ApiPath,
  Method extends ApiMethod,
  Status extends SuccessStatus = Extract<
    keyof OperationResponses<ApiOperation<Path, Method>>,
    SuccessStatus
  >,
> = Status extends keyof OperationResponses<ApiOperation<Path, Method>>
  ? ResponseContent<OperationResponses<ApiOperation<Path, Method>>[Status]>
  : never;
