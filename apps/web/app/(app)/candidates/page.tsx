import { AddCandidateDialog } from "@/components/candidates/add-candidate-dialog";
import { BulkUploadDialog } from "@/components/candidates/bulk-upload-dialog";
import { CandidatesList } from "@/components/candidates/candidates-list";
import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { apiGet, apiGetAll, apiGetAllPage } from "@/lib/api-client";
import {
  candidateFiltersFromSearchParams,
  candidateFiltersToSearchParams,
  type CandidateSearchParamInput,
} from "@/lib/candidate-search";
import { getMessages } from "@/lib/i18n";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getCandidateWorkspaceAccess } from "@/lib/permissions";
import type {
  CandidateDuplicatePage,
  CandidateSavedView,
  CandidateSearchPage,
  CommunicationCapabilities,
  Folder,
  Position,
} from "@/lib/types";

export const dynamic = "force-dynamic";

const EMPTY_SEARCH_PAGE: CandidateSearchPage = {
  items: [],
  total: 0,
  available_sources: [],
  next_cursor: null,
};

const EMPTY_DUPLICATES: CandidateDuplicatePage = {
  items: [],
  candidate_count: 0,
  next_cursor: null,
};

/** @ui-only Next.js candidate-list route properties. */
type Props = {
  searchParams: Promise<CandidateSearchParamInput>;
};

function redirectFailure(result: PromiseSettledResult<unknown>): unknown | null {
  if (result.status !== "rejected") return null;
  const cause = result.reason as { digest?: string };
  return cause?.digest?.startsWith("NEXT_REDIRECT") ? result.reason : null;
}

export default async function CandidatesIndexPage({ searchParams }: Props) {
  const rawParams = await searchParams;
  const filters = candidateFiltersFromSearchParams(rawParams);
  const query = candidateFiltersToSearchParams(filters, { limit: 50 });
  const [m, permissionMap] = await Promise.all([
    getMessages(),
    getCurrentPermissionMap(),
  ]);
  const access = getCandidateWorkspaceAccess(permissionMap);

  const [
    searchResult,
    positionsResult,
    foldersResult,
    viewsResult,
    duplicatesResult,
    communicationsCapabilitiesResult,
  ] =
    await Promise.allSettled([
      apiGet<CandidateSearchPage>(`/candidate-search?${query.toString()}`),
      apiGetAll<Position>("/positions"),
      apiGetAll<Folder>("/folders"),
      apiGetAll<CandidateSavedView>("/candidate-views"),
      apiGetAllPage<CandidateDuplicatePage["items"][number], CandidateDuplicatePage>(
        "/candidate-duplicates",
      ),
      apiGet<CommunicationCapabilities>("/communications/capabilities"),
    ]);

  for (const result of [
    searchResult,
    positionsResult,
    foldersResult,
    viewsResult,
    duplicatesResult,
    communicationsCapabilitiesResult,
  ]) {
    const redirect = redirectFailure(result);
    if (redirect) throw redirect;
  }

  const page =
    searchResult.status === "fulfilled" ? searchResult.value : EMPTY_SEARCH_PAGE;
  const positions =
    positionsResult.status === "fulfilled" ? positionsResult.value : [];
  const folderTree =
    foldersResult.status === "fulfilled" ? foldersResult.value : [];
  const savedViews =
    viewsResult.status === "fulfilled" ? viewsResult.value : [];
  const requestedViewId = Array.isArray(rawParams.view)
    ? rawParams.view[0]
    : rawParams.view;
  const selectedViewId = savedViews.some(
    (view) => view.id === requestedViewId,
  )
    ? requestedViewId ?? null
    : null;
  const duplicates =
    duplicatesResult.status === "fulfilled"
      ? duplicatesResult.value
      : EMPTY_DUPLICATES;
  const duplicatesLoadFailed = duplicatesResult.status === "rejected";
  const searchError =
    searchResult.status === "rejected"
      ? (searchResult.reason as Error).message
      : null;
  const synchronousRecipientLimit =
    communicationsCapabilitiesResult.status === "fulfilled"
      ? communicationsCapabilitiesResult.value.synchronous_recipient_limit
      : null;
  const durableRecipientLimit =
    communicationsCapabilitiesResult.status === "fulfilled"
      ? communicationsCapabilitiesResult.value.durable_recipient_limit
      : null;

  const folders = Array.from(
    new Set(
      positions
        .filter((position) => position.status === "open")
        .map((position) => position.folder?.trim())
        .filter((folder): folder is string => Boolean(folder)),
    ),
  ).sort((left, right) => left.localeCompare(right));
  const openCount = positions.filter((position) => position.status === "open").length;

  return (
    <>
      <Topbar
        crumbs={[{ label: m.candidates.crumb }]}
        actions={
          <>
            <LocaleSwitcher />
            {access.writeCandidates ? (
              <>
                <BulkUploadDialog
                  folders={folders}
                  folderTree={folderTree}
                  openPositionCount={openCount}
                />
                <AddCandidateDialog
                  folders={folders}
                  folderTree={folderTree}
                  openPositionCount={openCount}
                />
              </>
            ) : null}
          </>
        }
      />
      <div className="flex-1 overflow-y-auto overflow-x-hidden px-7 py-8">
        <header className="mb-7">
          <h1 className="mb-1 text-[24px] font-semibold tracking-[-0.025em]">
            {m.candidates.title}
          </h1>
          <p className="text-[13px] text-text-muted">
            {m.candidates.explorer.results(page.total)}
          </p>
        </header>

        <CandidatesList
          initialPage={page}
          initialFilters={filters}
          positions={positions}
          initialSavedViews={savedViews}
          initialSelectedViewId={selectedViewId}
          initialDuplicates={duplicates}
          initialDuplicatesLoadFailed={duplicatesLoadFailed}
          initialLoadError={searchError}
          access={access}
          synchronousRecipientLimit={synchronousRecipientLimit}
          durableRecipientLimit={durableRecipientLimit}
        />
      </div>
    </>
  );
}
