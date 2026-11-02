import { ClipboardCheck } from "lucide-react";
import { notFound, unstable_rethrow } from "next/navigation";

import { auth } from "@/auth";
import { Topbar } from "@/components/app/topbar";
import { AdminAssessmentWorkspace } from "@/components/assessments/admin-assessment-workspace";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import type { components } from "@/lib/generated/api";
import type { ApiResponseSchema } from "@/lib/generated/contracts";
import { getLocale } from "@/lib/i18n";
import { hasEveryPermission, hasPermission } from "@/lib/permissions";
import type { CandidateSummary, Position } from "@/lib/types";

type AssessmentTemplate = components["schemas"]["AssessmentTemplateOut"];
type Application = components["schemas"]["ApplicationOut"];
type ApplicationPage = ApiResponseSchema<"ApplicationPage">;

export const dynamic = "force-dynamic";

export default async function AssessmentsPage() {
  const [session, permissions, language] = await Promise.all([
    auth(),
    getCurrentPermissionMap(),
    getLocale(),
  ]);
  const canManage = hasPermission(permissions, "assessments.manage");
  const canReview = hasEveryPermission(permissions, [
    "assessments.grade",
    "assessments.results",
  ]);
  if (
    !session?.user?.id ||
    !session.user.organization?.id ||
    !hasEveryPermission(permissions, [
      "assessments.read",
      "applications.read",
      "candidates.read",
    ]) ||
    (!canManage && !canReview)
  ) {
    notFound();
  }
  const [templatesResult, applicationsResult, candidatesResult, positionsResult] =
    await Promise.allSettled([
      apiGetAll<AssessmentTemplate>("/assessment-templates?include_archived=true"),
      apiGet<ApplicationPage>("/applications?limit=200"),
      apiGetAll<CandidateSummary>("/candidates"),
      apiGetAll<Position>("/positions"),
    ]);

  for (const result of [
    templatesResult,
    applicationsResult,
    candidatesResult,
    positionsResult,
  ]) {
    if (result.status === "rejected") unstable_rethrow(result.reason);
  }

  const summaries = templatesResult.status === "fulfilled" ? templatesResult.value : [];
  const detailResults = await Promise.allSettled(
    summaries.map((template) =>
      apiGet<AssessmentTemplate>(`/assessment-templates/${encodeURIComponent(template.id)}`),
    ),
  );
  const detailById = new Map(
    detailResults.flatMap((result) =>
      result.status === "fulfilled" ? [[result.value.id, result.value] as const] : [],
    ),
  );
  const templates = summaries.map((template) => detailById.get(template.id) ?? template);

  const accessError =
    templatesResult.status === "rejected"
      ? language === "ru"
        ? "Не удалось загрузить шаблоны. Проверьте право управления оцениваниями."
        : "Assessment templates could not be loaded. Check your assessment-management permission."
      : null;

  return (
    <>
      <Topbar
        crumbs={[
          {
            label: language === "ru" ? "Оценивания" : "Assessments",
          },
        ]}
      />
      <div className="flex-1 overflow-y-auto overflow-x-hidden px-4 py-6 sm:px-7 sm:py-8">
        <header className="mb-6 flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 items-center justify-center rounded-lg bg-accent-muted text-primary">
            <ClipboardCheck className="h-4 w-4" aria-hidden />
          </span>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              {language === "ru" ? "Оценивания кандидатов" : "Candidate assessments"}
            </h1>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-text-muted">
              {language === "ru"
                ? "Версионные шаблоны, условная логика, персональные приглашения и результаты."
                : "Versioned templates, conditional logic, private invitations, and results."}
            </p>
          </div>
        </header>
        <AdminAssessmentWorkspace
          initialTemplates={templates}
          initialApplications={
            applicationsResult.status === "fulfilled"
              ? applicationsResult.value.items
              : []
          }
          initialApplicationsNextCursor={
            applicationsResult.status === "fulfilled"
              ? applicationsResult.value.next_cursor
              : null
          }
          candidates={candidatesResult.status === "fulfilled" ? candidatesResult.value : []}
          positions={positionsResult.status === "fulfilled" ? positionsResult.value : []}
          language={language}
          draftScope={`${session.user.organization.id}:${session.user.id}`}
          canManage={canManage}
          canReview={canReview}
          accessError={accessError}
          relatedDataError={
            [applicationsResult, candidatesResult, positionsResult].some(
              (result) => result.status === "rejected",
            )
          }
        />
      </div>
    </>
  );
}
