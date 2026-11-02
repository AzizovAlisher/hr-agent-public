import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import test from "node:test";

import {
  analyzeBoundarySource,
  discoverBoundaryFiles,
  verifyBoundaryDeclarations,
} from "./check-api-contracts.mjs";

test("discovers every app, component, and lib TypeScript boundary", async (context) => {
  const root = await mkdtemp(join(tmpdir(), "hr-contract-discovery-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  await Promise.all([
    mkdir(join(root, "app/api/example"), { recursive: true }),
    mkdir(join(root, "components"), { recursive: true }),
    mkdir(join(root, "lib"), { recursive: true }),
  ]);
  await Promise.all([
    writeFile(join(root, "app/api/example/route.ts"), "export async function GET() {}"),
    writeFile(join(root, "app/page.tsx"), "export default function Page() {}"),
    writeFile(join(root, "app/ignored.ts"), "export const included = true"),
    writeFile(join(root, "components/example.tsx"), "export function Example() {}"),
    writeFile(join(root, "components/model.ts"), "export const model = true"),
    writeFile(join(root, "lib/example.ts"), "export const value = true"),
    writeFile(join(root, "lib/example.tsx"), "export function LibView() {}"),
  ]);

  const discovered = (await discoverBoundaryFiles(root)).map((file) =>
    relative(root, file),
  );

  assert.ok(discovered.includes("app/api/example/route.ts"));
  assert.ok(discovered.includes("app/page.tsx"));
  assert.ok(discovered.includes("app/ignored.ts"));
  assert.ok(discovered.includes("components/example.tsx"));
  assert.ok(discovered.includes("components/model.ts"));
  assert.ok(discovered.includes("lib/example.ts"));
  assert.ok(discovered.includes("lib/example.tsx"));
});

test("rejects an imported UI-only response alias across discovered files", async (context) => {
  const root = await mkdtemp(join(tmpdir(), "hr-contract-provenance-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  await Promise.all([
    mkdir(join(root, "app/api/candidates"), { recursive: true }),
    mkdir(join(root, "lib"), { recursive: true }),
  ]);
  await Promise.all([
    writeFile(
      join(root, "lib/view-types.ts"),
      `
        /** @ui-only Candidate card projection that must never type API data. */
        export interface CandidateResponse { id: string; }
      `,
    ),
    writeFile(
      join(root, "app/api/candidates/route.ts"),
      `
        import type { CandidateResponse } from "@/lib/view-types";

        export async function GET(response: Response) {
          return (await response.json()) as CandidateResponse;
        }
      `,
    ),
  ]);

  await assert.rejects(
    verifyBoundaryDeclarations(root),
    /CandidateResponse.*import provenance is documented local\/UI-only/,
  );
});

async function expectImportedViewResponseRejection(context, routeSource, pattern) {
  const root = await mkdtemp(join(tmpdir(), "hr-contract-adjacent-bypass-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  await Promise.all([
    mkdir(join(root, "app/api/candidates"), { recursive: true }),
    mkdir(join(root, "lib"), { recursive: true }),
  ]);
  await Promise.all([
    writeFile(
      join(root, "lib/view-types.ts"),
      `
        /** @ui-only Candidate card projection that must never type API data. */
        export interface CandidateResponse { id: string; }
      `,
    ),
    writeFile(join(root, "app/api/candidates/route.ts"), routeSource),
  ]);
  await assert.rejects(verifyBoundaryDeclarations(root), pattern);
}

async function verifyFixture(context, files) {
  const root = await mkdtemp(join(tmpdir(), "hr-contract-flow-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  await Promise.all(
    Object.keys(files).map((file) => mkdir(join(root, file, ".."), { recursive: true })),
  );
  await Promise.all(
    Object.entries(files).map(([file, source]) => writeFile(join(root, file), source)),
  );
  return { root, verification: verifyBoundaryDeclarations(root) };
}

const VIEW_TYPES = `
  /** @ui-only Candidate card projection that must never type API data. */
  export interface CandidateResponse { id: string; }
`;

test("rejects a namespace-imported UI-only response assertion", async (context) => {
  await expectImportedViewResponseRejection(
    context,
    `
      import type * as View from "@/lib/view-types";
      export async function GET(response: Response) {
        return (await response.json()) as View.CandidateResponse;
      }
    `,
    /View\.CandidateResponse.*import provenance is documented local\/UI-only/,
  );
});

test("rejects an angle-bracket UI-only response assertion", async (context) => {
  await expectImportedViewResponseRejection(
    context,
    `
      import type { CandidateResponse } from "@/lib/view-types";
      export async function GET(response: Response) {
        return <CandidateResponse>await response.json();
      }
    `,
    /CandidateResponse.*response\.json\(\).*documented local\/UI-only/,
  );
});

test("rejects a UI-only typed response assignment", async (context) => {
  await expectImportedViewResponseRejection(
    context,
    `
      import type { CandidateResponse } from "@/lib/view-types";
      export async function GET(response: Response) {
        const payload: CandidateResponse = await response.json();
        return payload;
      }
    `,
    /CandidateResponse.*response\.json\(\) assignment.*documented local\/UI-only/,
  );
});

test("rejects any as a legacy API response type", async (context) => {
  await expectImportedViewResponseRejection(
    context,
    `
      import { apiGet } from "@/lib/api-client";
      export async function GET() {
        return apiGet<any>("/candidates/1");
      }
    `,
    /any response type used by apiGet/,
  );
});

test("rejects a UI-only response through an aliased transport import", async (context) => {
  await expectImportedViewResponseRejection(
    context,
    `
      import { apiGet as load } from "@/lib/api-client";
      import type { CandidateResponse } from "@/lib/view-types";
      export async function GET() {
        return load<CandidateResponse>("/candidates/1");
      }
    `,
    /CandidateResponse.*load response type.*documented local\/UI-only/,
  );
});

test("rejects a UI-only response through a generic transport wrapper", async (context) => {
  await expectImportedViewResponseRejection(
    context,
    `
      import { apiGet } from "@/lib/api-client";
      import type { CandidateResponse } from "@/lib/view-types";
      async function optional<T>(path: string): Promise<T> {
        return apiGet<T>(path);
      }
      export async function GET() {
        return optional<CandidateResponse>("/candidates/1");
      }
    `,
    /CandidateResponse.*optional response type.*documented local\/UI-only/,
  );
});

test("runs transport analysis in lib and app action TypeScript boundaries", async (context) => {
  const { verification } = await verifyFixture(context, {
    "lib/view-types.ts": VIEW_TYPES,
    "lib/unsafe-client.ts": `
      import type { CandidateResponse } from "@/lib/view-types";
      export async function load(response: Response) {
        return (await response.json()) as CandidateResponse;
      }
    `,
    "app/(app)/candidate/actions.ts": `
      import type { CandidateResponse } from "@/lib/view-types";
      export async function action(response: Response) {
        return (await response.json()) as CandidateResponse;
      }
    `,
    "app/unsafe-helper.ts": `
      import type { CandidateResponse } from "@/lib/view-types";
      export async function load(response: Response) {
        return (await response.json()) as CandidateResponse;
      }
    `,
  });
  await assert.rejects(
    verification,
    (error) =>
      error instanceof Error &&
      error.message.includes("lib/unsafe-client.ts") &&
      error.message.includes("app/(app)/candidate/actions.ts") &&
      error.message.includes("app/unsafe-helper.ts"),
  );
});

test("rejects unknown and object legacy response keywords", async (context) => {
  const { verification } = await verifyFixture(context, {
    "app/api/candidates/route.ts": `
      import { apiGet } from "@/lib/api-client";
      export async function GET() {
        await apiGet<unknown>("/candidates/1");
        return apiGet<object>("/candidates/2");
      }
    `,
  });
  await assert.rejects(
    verification,
    (error) =>
      error instanceof Error &&
      error.message.includes("unknown response type used by apiGet") &&
      error.message.includes("object response type used by apiGet"),
  );
});

test("rejects satisfies on response JSON with a UI-only type", async (context) => {
  const { verification } = await verifyFixture(context, {
    "lib/view-types.ts": VIEW_TYPES,
    "app/api/candidates/route.ts": `
      import type { CandidateResponse } from "@/lib/view-types";
      export async function GET(response: Response) {
        return (await response.json()) satisfies CandidateResponse;
      }
    `,
  });
  await assert.rejects(
    verification,
    /CandidateResponse.*response\.json\(\) satisfies.*documented local\/UI-only/,
  );
});

test("rejects double response casts through unknown", async (context) => {
  const { verification } = await verifyFixture(context, {
    "lib/view-types.ts": VIEW_TYPES,
    "app/api/candidates/route.ts": `
      import type { CandidateResponse } from "@/lib/view-types";
      export async function GET(response: Response) {
        return ((await response.json()) as unknown) as CandidateResponse;
      }
    `,
  });
  await assert.rejects(
    verification,
    (error) =>
      error instanceof Error &&
      error.message.includes("unknown response type used by response.json()") &&
      error.message.includes("import provenance is documented local/UI-only"),
  );
});

test("rejects Promise UI-only return annotations fed by response JSON", async (context) => {
  const { verification } = await verifyFixture(context, {
    "lib/view-types.ts": VIEW_TYPES,
    "app/api/candidates/route.ts": `
      import type { CandidateResponse } from "@/lib/view-types";
      export async function GET(response: Response): Promise<CandidateResponse> {
        return response.json();
      }
    `,
  });
  await assert.rejects(
    verification,
    /CandidateResponse.*return annotation.*documented local\/UI-only/,
  );
});

test("rejects nested generic transport wrappers", async (context) => {
  const { verification } = await verifyFixture(context, {
    "lib/view-types.ts": VIEW_TYPES,
    "app/api/candidates/route.ts": `
      import { apiGet } from "@/lib/api-client";
      import type { CandidateResponse } from "@/lib/view-types";
      async function outer<T>(path: string): Promise<T> {
        async function inner<U>(innerPath: string): Promise<U> {
          return apiGet<U>(innerPath);
        }
        return inner<T>(path);
      }
      export async function GET() {
        return outer<CandidateResponse>("/candidates/1");
      }
    `,
  });
  await assert.rejects(
    verification,
    /CandidateResponse.*outer response type.*documented local\/UI-only/,
  );
});

test("rejects cross-file generic transport wrappers", async (context) => {
  const { verification } = await verifyFixture(context, {
    "lib/view-types.ts": VIEW_TYPES,
    "lib/optional.ts": `
      import { apiGet } from "@/lib/api-client";
      export async function optional<T>(path: string): Promise<T> {
        return apiGet<T>(path);
      }
    `,
    "app/api/candidates/route.ts": `
      import { optional } from "@/lib/optional";
      import type { CandidateResponse } from "@/lib/view-types";
      export async function GET() {
        return optional<CandidateResponse>("/candidates/1");
      }
    `,
  });
  await assert.rejects(
    verification,
    /CandidateResponse.*optional response type.*documented local\/UI-only/,
  );
});

test("rejects namespace and local aliases of legacy transports", async (context) => {
  const { verification } = await verifyFixture(context, {
    "lib/view-types.ts": VIEW_TYPES,
    "app/api/candidates/route.ts": `
      import * as Api from "@/lib/api-client";
      import type { CandidateResponse } from "@/lib/view-types";
      const load = Api.apiGet;
      export async function GET() {
        await Api.apiGet<CandidateResponse>("/candidates/1");
        return load<CandidateResponse>("/candidates/2");
      }
    `,
  });
  await assert.rejects(
    verification,
    (error) =>
      error instanceof Error &&
      error.message.includes("Api.apiGet response type") &&
      error.message.includes("load response type"),
  );
});

test("rejects response JSON after an intermediate variable and generic wrapper", async (context) => {
  const { verification } = await verifyFixture(context, {
    "lib/view-types.ts": VIEW_TYPES,
    "lib/decode.ts": `
      export async function decode<T>(response: Response): Promise<T> {
        const payload = await response.json();
        return payload as T;
      }
    `,
    "app/api/candidates/route.ts": `
      import { decode } from "@/lib/decode";
      import type { CandidateResponse } from "@/lib/view-types";
      export async function GET(response: Response) {
        const payload = await response.json();
        const direct = payload as CandidateResponse;
        return { direct, wrapped: await decode<CandidateResponse>(response) };
      }
    `,
  });
  await assert.rejects(
    verification,
    (error) =>
      error instanceof Error &&
      error.message.includes("response.json()") &&
      error.message.includes("decode response type") &&
      error.message.includes("documented local/UI-only"),
  );
});

test("accepts generated types through cross-file, namespace, alias, and JSON flows", async (context) => {
  const { verification } = await verifyFixture(context, {
    "lib/api-types.ts": `
      import type { ApiResponseSchema } from "@/lib/generated/contracts";
      export type CandidateResponse = ApiResponseSchema<"CandidateOut">;
    `,
    "lib/optional.ts": `
      import * as Api from "@/lib/api-client";
      export async function optional<T>(path: string): Promise<T> {
        const load = Api.apiGet;
        return load<T>(path);
      }
    `,
    "app/api/candidates/route.ts": `
      import * as Api from "@/lib/api-client";
      import type { CandidateResponse } from "@/lib/api-types";
      import { optional } from "@/lib/optional";
      const load = Api.apiGet;
      export async function GET(response: Response) {
        const assigned: CandidateResponse = await response.json();
        const direct = await load<CandidateResponse>("/candidates/1");
        const wrapped = await optional<CandidateResponse>("/candidates/2");
        return { assigned, direct, wrapped };
      }
    `,
  });
  await assert.doesNotReject(verification);
});

test("rejects a handwritten API response DTO declared in TSX", () => {
  const source = `
    interface CandidateResponse {
      id: string;
      email: string;
    }

    async function loadCandidate() {
      return apiGet<CandidateResponse>("/candidates/1");
    }
  `;

  const result = analyzeBoundarySource("components/candidate.tsx", source);

  assert.match(result.violations.join("\n"), /CandidateResponse/);
  assert.match(result.violations.join("\n"), /used as apiGet response type/);
});

test("accepts a generated response alias in TSX", () => {
  const source = `
    type CandidateResponse = components["schemas"]["CandidateDetail"];

    async function loadCandidate() {
      return apiGet<CandidateResponse>("/candidates/1");
    }
  `;

  const result = analyzeBoundarySource("components/candidate.tsx", source);

  assert.deepEqual(result.violations, []);
});

test("accepts a documented UI-only declaration that never crosses transport", () => {
  const source = `
    /** @ui-only Whether the local details drawer is expanded. */
    type DrawerState = { open: boolean };
  `;

  const result = analyzeBoundarySource("components/candidate.tsx", source);

  assert.deepEqual(result.violations, []);
});

test("requires a reason on the direct UI-only suppression", () => {
  const source = `
    /** @ui-only */
    type DrawerState = { open: boolean };
  `;

  const result = analyzeBoundarySource("components/candidate.tsx", source);

  assert.match(result.violations.join("\n"), /DrawerState/);
});

test("does not mistake an ordinary components property for generated backing", () => {
  const source = `
    interface HandwrittenResponse {
      components: string[];
    }
  `;

  const result = analyzeBoundarySource("components/candidate.tsx", source);

  assert.match(result.violations.join("\n"), /HandwrittenResponse/);
});

test("does not let UI-only suppression hide a transport response DTO", () => {
  const source = `
    /** @ui-only This attempted suppression must not bypass transport checks. */
    interface CandidateResponse {
      id: string;
    }

    async function loadCandidate() {
      return apiGet<CandidateResponse>("/candidates/1");
    }
  `;

  const result = analyzeBoundarySource("components/candidate.tsx", source);

  assert.match(result.violations.join("\n"), /used as apiGet response type/);
});

test("rejects inline handwritten legacy transport response shapes", () => {
  const source = `
    async function loadCandidate() {
      return apiGet<{ id: string }>("/candidates/1");
    }
  `;

  const result = analyzeBoundarySource("components/candidate.tsx", source);

  assert.match(result.violations.join("\n"), /inline response type used by apiGet/);
});

test("rejects inline response.json assertions in TSX", () => {
  const source = `
    async function loadCandidate(response: Response) {
      return (await response.json()) as { id: string };
    }
  `;

  const result = analyzeBoundarySource("components/candidate.tsx", source);

  assert.match(
    result.violations.join("\n"),
    /inline response type used by response\.json\(\)/,
  );
});

test("accepts generated-backed types across wrapper and assignment patterns", () => {
  const source = `
    type CandidateResponse = components["schemas"]["CandidateDetail"];
    async function optional<T>(path: string): Promise<T> {
      return apiGet<T>(path);
    }
    async function load(response: Response) {
      const assigned: CandidateResponse = await response.json();
      const wrapped = await optional<CandidateResponse>("/candidates/1");
      return { assigned, wrapped };
    }
  `;

  const result = analyzeBoundarySource("app/api/candidates/route.ts", source);

  assert.deepEqual(result.violations, []);
});

test("AI attachment picker advertises exactly the API-supported suffixes", async () => {
  const [formSource, apiSource] = await Promise.all([
    readFile(
      new URL("../components/ai/new-mission-form.tsx", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../../api/app/services/ai_hiring_attachments.py", import.meta.url),
      "utf8",
    ),
  ]);
  const accept = formSource.match(/accept="([^"]+)"/)?.[1];
  const allowed = apiSource.match(/_ALLOWED_SUFFIXES\s*=\s*\{([^}]+)\}/s)?.[1];
  assert.ok(accept, "AI attachment input must declare accepted formats");
  assert.ok(allowed, "API attachment suffix contract must remain discoverable");

  const uiSuffixes = accept
    .split(",")
    .filter((value) => value.startsWith("."))
    .sort();
  const apiSuffixes = [...allowed.matchAll(/["'](\.[a-z0-9]+)["']/g)]
    .map((match) => match[1])
    .sort();

  assert.deepEqual(uiSuffixes, apiSuffixes);
  assert.ok(!uiSuffixes.includes(".doc"));
});

test("candidate upload copy stays aligned with the canonical API byte limit", async () => {
  const [
    english,
    russian,
    careersGuide,
    entitlementSource,
    legacyPrototype,
    clientActions,
  ] = await Promise.all([
    readFile(new URL("../lib/i18n/messages-en.ts", import.meta.url), "utf8"),
    readFile(new URL("../lib/i18n/messages-ru.ts", import.meta.url), "utf8"),
    readFile(
      new URL("../../../docs/CAREERS_API_INTEGRATION_RU.md", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../../api/app/services/entitlements.py", import.meta.url),
      "utf8",
    ),
    readFile(new URL("../../../index.html", import.meta.url), "utf8"),
    readFile(new URL("../lib/actions.ts", import.meta.url), "utf8"),
  ]);
  const limitMatch = entitlementSource.match(
    /"candidate_document_bytes":\s*(\d+)\s*\*\s*1024\s*\*\s*1024/,
  );
  assert.ok(limitMatch, "canonical candidate document limit must remain discoverable");

  const limitMiB = Number(limitMatch[1]);
  assert.match(english, new RegExp(`PDF, DOCX · up to ${limitMiB} MiB`));
  assert.match(russian, new RegExp(`PDF, DOCX · до ${limitMiB} МиБ`));
  assert.match(careersGuide, new RegExp(`PDF или DOCX, до ${limitMiB} МиБ`));
  assert.match(legacyPrototype, new RegExp(`PDF, DOCX · up to ${limitMiB} MiB`));
  assert.match(
    clientActions,
    new RegExp(`CV file exceeds the ${limitMiB} MiB upload limit\\.`),
  );
  assert.doesNotMatch(english, /PDF, DOCX · up to 20\s*(?:MB|MiB)/);
  assert.doesNotMatch(russian, /PDF, DOCX · до 20\s*(?:МБ|МиБ)/);
});

test("every ranked-candidate view prefers the policy-filtered display name", async () => {
  const sources = await Promise.all(
    [
      "../components/positions/candidate-row.tsx",
      "../components/positions/kanban-board.tsx",
      "../app/(app)/positions/[id]/compare/page.tsx",
    ].map((path) => readFile(new URL(path, import.meta.url), "utf8")),
  );

  for (const source of sources) {
    assert.match(source, /display_name\?\.trim\(\)\s*\|\|\s*candRef\(/s);
  }
});
