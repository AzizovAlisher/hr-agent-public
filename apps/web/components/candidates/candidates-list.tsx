"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useTransition,
} from "react";
import {
  ArrowRight,
  Bookmark,
  ChevronDown,
  Download,
  Eye,
  Filter,
  GitMerge,
  Loader2,
  Mail,
  Search,
  Tags,
  UsersRound,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { DispositionDialog } from "@/components/applications/disposition-dialog";
import { CandidateMergeDialog } from "@/components/candidates/candidate-merge-dialog";
import { BulkMessageDialog } from "@/components/communications/bulk-message-dialog";
import { DeleteCandidateButton } from "@/components/candidates/delete-candidate-button";
import { FavoriteButton } from "@/components/candidates/favorite-button";
import { useI18n } from "@/components/i18n/locale-provider";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { ScoreChip } from "@/components/ui/score-chip";
import { SourceBadge } from "@/components/ui/source-badge";
import {
  applyCandidateBulkAction,
  createCandidateViewAction,
  deleteCandidateViewAction,
  listCandidateDuplicatesAction,
  listCandidateSearchAction,
  updateCandidateViewAction,
} from "@/lib/actions";
import {
  candidateFilterCount,
  candidateFiltersKey,
  candidateFiltersToSearchParams,
  EMPTY_CANDIDATE_FILTERS,
} from "@/lib/candidate-search";
import { candDisplayRef, candRef } from "@/lib/format";
import type {
  CandidateBulkRequest,
  CandidateDuplicateGroup,
  CandidateDuplicatePage,
  CandidateMergeResult,
  CandidateSavedView,
  CandidateSearchFilters,
  CandidateSearchItem,
  CandidateSearchPage,
  KanbanStage,
  Position,
} from "@/lib/types";
import type { CandidateWorkspaceAccess } from "@/lib/permissions";
import { KANBAN_STAGES } from "@/lib/types";
import { cn } from "@/lib/utils";

/** @ui-only Candidate-list composition properties. */
type Props = {
  initialPage: CandidateSearchPage;
  initialFilters: CandidateSearchFilters;
  positions: Position[];
  initialSavedViews: CandidateSavedView[];
  initialSelectedViewId: string | null;
  initialDuplicates: CandidateDuplicatePage;
  initialDuplicatesLoadFailed: boolean;
  initialLoadError: string | null;
  access: CandidateWorkspaceAccess;
  synchronousRecipientLimit: number | null;
  durableRecipientLimit: number | null;
};

const controlClass =
  "rounded-[6px] border border-border bg-bg-elevated px-2.5 py-[7px] text-[12px] text-text outline-none transition-colors hover:border-border-strong focus:border-border-strong";

function uniqueItems(items: CandidateSearchItem[]): CandidateSearchItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

export function CandidatesList({
  initialPage,
  initialFilters,
  positions,
  initialSavedViews,
  initialSelectedViewId,
  initialDuplicates,
  initialDuplicatesLoadFailed,
  initialLoadError,
  access,
  synchronousRecipientLimit,
  durableRecipientLimit,
}: Props) {
  const { m } = useI18n();
  const router = useRouter();
  const [filters, setFilters] = useState(initialFilters);
  const [items, setItems] = useState(initialPage.items);
  const [total, setTotal] = useState(initialPage.total);
  const [nextCursor, setNextCursor] = useState(initialPage.next_cursor);
  const [availableSources, setAvailableSources] = useState(
    initialPage.available_sources,
  );
  const [query, setQuery] = useState(initialFilters.q ?? "");
  const [scoreMin, setScoreMin] = useState(
    initialFilters.score_min?.toString() ?? "",
  );
  const [scoreMax, setScoreMax] = useState(
    initialFilters.score_max?.toString() ?? "",
  );
  const [tagFilter, setTagFilter] = useState(initialFilters.tags.join(", "));
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [savedViews, setSavedViews] = useState(initialSavedViews);
  const [selectedViewId, setSelectedViewId] = useState(
    initialSelectedViewId ?? "",
  );
  const [saveDialogOpen, setSaveDialogOpen] = useState(false);
  const [viewName, setViewName] = useState("");
  const [duplicatesOpen, setDuplicatesOpen] = useState(false);
  const [duplicates, setDuplicates] = useState(initialDuplicates);
  const [duplicatesLoadFailed, setDuplicatesLoadFailed] = useState(
    initialDuplicatesLoadFailed,
  );
  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeIds, setMergeIds] = useState<string[]>([]);
  const [bulkMessageOpen, setBulkMessageOpen] = useState(false);
  const [bulkRejectOpen, setBulkRejectOpen] = useState(false);
  const [bulkTag, setBulkTag] = useState("");
  const [bulkPositionId, setBulkPositionId] = useState("");
  const [bulkStage, setBulkStage] = useState<KanbanStage>("screening");
  const [isNavigating, startNavigation] = useTransition();
  const [isLoadingMore, startLoadingMore] = useTransition();
  const [isSavingView, startSavingView] = useTransition();
  const [isLoadingDuplicates, startLoadingDuplicates] = useTransition();
  const [isBulkPending, startBulk] = useTransition();
  const [isExporting, setIsExporting] = useState(false);
  const [includeContactsInExport, setIncludeContactsInExport] = useState(false);
  const initialKey = candidateFiltersKey(initialFilters);
  const queryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeFiltersKey = useRef(initialKey);
  const selectedViewIdRef = useRef(initialSelectedViewId ?? "");
  const loadMoreGeneration = useRef(0);
  const canSelect =
    access.writeCandidates ||
    access.exportCandidates ||
    access.sendCandidateMessages;

  useEffect(() => {
    activeFiltersKey.current = initialKey;
    selectedViewIdRef.current = initialSelectedViewId ?? "";
    loadMoreGeneration.current += 1;
    setFilters(initialFilters);
    setItems(initialPage.items);
    setTotal(initialPage.total);
    setNextCursor(initialPage.next_cursor);
    setAvailableSources(initialPage.available_sources);
    setQuery(initialFilters.q ?? "");
    setScoreMin(initialFilters.score_min?.toString() ?? "");
    setScoreMax(initialFilters.score_max?.toString() ?? "");
    setTagFilter(initialFilters.tags.join(", "));
    setSelectedViewId(initialSelectedViewId ?? "");
    setSelectedIds(new Set());
    setIncludeContactsInExport(false);
  }, [initialFilters, initialKey, initialPage, initialSelectedViewId]);

  useEffect(() => {
    setSavedViews(initialSavedViews);
  }, [initialSavedViews]);

  useEffect(() => {
    setDuplicates(initialDuplicates);
    setDuplicatesLoadFailed(initialDuplicatesLoadFailed);
  }, [initialDuplicates, initialDuplicatesLoadFailed]);

  const navigate = useCallback(
    (
      nextFilters: CandidateSearchFilters,
      options?: { selectedViewId?: string | null },
    ) => {
      const nextSelectedViewId =
        options && "selectedViewId" in options
          ? options.selectedViewId ?? ""
          : selectedViewIdRef.current;
      activeFiltersKey.current = candidateFiltersKey(nextFilters);
      selectedViewIdRef.current = nextSelectedViewId;
      loadMoreGeneration.current += 1;
      setFilters(nextFilters);
      setSelectedViewId(nextSelectedViewId);
      setSelectedIds(new Set());
      const params = candidateFiltersToSearchParams(nextFilters);
      if (nextSelectedViewId) params.set("view", nextSelectedViewId);
      const destination = params.size
        ? `/candidates?${params.toString()}`
        : "/candidates";
      startNavigation(() => router.replace(destination, { scroll: false }));
    },
    [router],
  );

  useEffect(() => {
    if (queryTimer.current) clearTimeout(queryTimer.current);
    const normalized = query.trim() || null;
    if (normalized === filters.q) return;
    queryTimer.current = setTimeout(() => {
      navigate({ ...filters, q: normalized });
    }, 350);
    return () => {
      if (queryTimer.current) clearTimeout(queryTimer.current);
    };
  }, [filters, navigate, query]);

  const sourceOptions = useMemo(
    () =>
      Array.from(new Set([...availableSources, ...filters.sources])).sort(
        (left, right) => left.localeCompare(right),
      ),
    [availableSources, filters.sources],
  );

  const openPositions = useMemo(
    () =>
      positions
        .filter((position) => position.status !== "closed")
        .sort((left, right) => left.title.localeCompare(right.title)),
    [positions],
  );

  const filterPositions = useMemo(
    () =>
      positions
        .filter(
          (position) =>
            position.status !== "closed" ||
            position.id === filters.position_id,
        )
        .sort((left, right) => left.title.localeCompare(right.title)),
    [filters.position_id, positions],
  );

  const selectedItems = useMemo(() => {
    const ids = selectedIds;
    return items.filter((item) => ids.has(item.id));
  }, [items, selectedIds]);

  const commonPositionIds = useMemo(() => {
    if (selectedItems.length === 0) return [];
    let common = new Set(
      selectedItems[0].applications.map((application) => application.position_id),
    );
    for (const item of selectedItems.slice(1)) {
      const itemPositions = new Set(
        item.applications.map((application) => application.position_id),
      );
      common = new Set([...common].filter((id) => itemPositions.has(id)));
    }
    return [...common];
  }, [selectedItems]);

  useEffect(() => {
    if (!bulkPositionId || !commonPositionIds.includes(bulkPositionId)) {
      setBulkPositionId(commonPositionIds[0] ?? "");
    }
  }, [bulkPositionId, commonPositionIds]);

  const allVisibleSelected =
    items.length > 0 && items.every((item) => selectedIds.has(item.id));
  const activeFilterCount = candidateFilterCount(filters);

  function patchFilters(patch: Partial<CandidateSearchFilters>) {
    navigate({ ...filters, ...patch });
  }

  function toggleArrayFilter<T extends string>(
    field: "sources" | "stages",
    value: T,
  ) {
    const current = filters[field] as T[];
    const next = current.includes(value)
      ? current.filter((entry) => entry !== value)
      : [...current, value];
    patchFilters({ [field]: next } as Partial<CandidateSearchFilters>);
  }

  function applyScores() {
    const parse = (value: string) => {
      if (value.trim() === "") return null;
      const number = Number(value);
      return Number.isFinite(number)
        ? Math.max(0, Math.min(100, Math.round(number)))
        : null;
    };
    let minimum = parse(scoreMin);
    let maximum = parse(scoreMax);
    if (minimum != null && maximum != null && minimum > maximum) {
      [minimum, maximum] = [maximum, minimum];
    }
    setScoreMin(minimum?.toString() ?? "");
    setScoreMax(maximum?.toString() ?? "");
    if (minimum !== filters.score_min || maximum !== filters.score_max) {
      patchFilters({ score_min: minimum, score_max: maximum });
    }
  }

  function applyTags() {
    const tags = Array.from(
      new Set(
        tagFilter
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean),
      ),
    ).sort();
    setTagFilter(tags.join(", "));
    if (JSON.stringify(tags) !== JSON.stringify(filters.tags)) {
      patchFilters({ tags });
    }
  }

  function clearFilters() {
    setQuery("");
    setScoreMin("");
    setScoreMax("");
    setTagFilter("");
    navigate(
      { ...EMPTY_CANDIDATE_FILTERS },
      { selectedViewId: null },
    );
  }

  function toggleCandidate(candidateId: string) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(candidateId)) {
        next.delete(candidateId);
      } else if (next.size < 100) {
        next.add(candidateId);
      } else {
        toast.error(m.candidates.explorer.bulk.selectionLimit);
      }
      return next;
    });
  }

  function toggleAllVisible() {
    setSelectedIds((current) => {
      if (allVisibleSelected) return new Set();
      return new Set(items.slice(0, 100).map((item) => item.id));
    });
  }

  function loadMore() {
    if (!nextCursor || isLoadingMore) return;
    const requestedFilters = filters;
    const requestedKey = candidateFiltersKey(requestedFilters);
    const requestedCursor = nextCursor;
    const requestGeneration = ++loadMoreGeneration.current;
    startLoadingMore(async () => {
      const result = await listCandidateSearchAction(
        requestedFilters,
        requestedCursor,
      );
      if (
        requestGeneration !== loadMoreGeneration.current ||
        requestedKey !== activeFiltersKey.current
      ) {
        return;
      }
      if (!result.ok) {
        toast.error(m.candidates.explorer.loadFailed);
        return;
      }
      setItems((current) => uniqueItems([...current, ...result.data.items]));
      setTotal(result.data.total);
      setAvailableSources(result.data.available_sources);
      setNextCursor(result.data.next_cursor);
    });
  }

  function applySavedView(viewId: string) {
    const view = savedViews.find((entry) => entry.id === viewId);
    if (!view) {
      navigate(filters, { selectedViewId: null });
      return;
    }
    setQuery(view.filters.q ?? "");
    setScoreMin(view.filters.score_min?.toString() ?? "");
    setScoreMax(view.filters.score_max?.toString() ?? "");
    setTagFilter(view.filters.tags.join(", "));
    navigate(view.filters, { selectedViewId: view.id });
  }

  function createSavedView() {
    const name = viewName.trim();
    if (!name || isSavingView) return;
    startSavingView(async () => {
      const result = await createCandidateViewAction(name, filters);
      if (!result.ok) {
        toast.error(m.candidates.explorer.viewFailed(result.error));
        return;
      }
      setSavedViews((current) => [...current, result.data]);
      navigate(filters, { selectedViewId: result.data.id });
      setSaveDialogOpen(false);
      setViewName("");
      toast.success(m.candidates.explorer.viewSaved);
    });
  }

  function updateSavedView() {
    if (!selectedViewId || isSavingView) return;
    startSavingView(async () => {
      const result = await updateCandidateViewAction(selectedViewId, { filters });
      if (!result.ok) {
        toast.error(m.candidates.explorer.viewFailed(result.error));
        return;
      }
      setSavedViews((current) =>
        current.map((view) => (view.id === result.data.id ? result.data : view)),
      );
      toast.success(m.candidates.explorer.viewUpdated);
    });
  }

  function deleteSavedView() {
    if (
      !selectedViewId ||
      isSavingView ||
      !window.confirm(m.candidates.explorer.confirmDeleteView)
    ) {
      return;
    }
    startSavingView(async () => {
      const result = await deleteCandidateViewAction(selectedViewId);
      if (!result.ok) {
        toast.error(m.candidates.explorer.viewFailed(result.error));
        return;
      }
      setSavedViews((current) =>
        current.filter((view) => view.id !== selectedViewId),
      );
      navigate(filters, { selectedViewId: null });
      toast.success(m.candidates.explorer.viewDeleted);
    });
  }

  function refreshDuplicates() {
    if (isLoadingDuplicates) return;
    setDuplicatesLoadFailed(false);
    startLoadingDuplicates(async () => {
      const result = await listCandidateDuplicatesAction();
      if (!result.ok) {
        setDuplicatesLoadFailed(true);
        return;
      }
      setDuplicates(result.data);
    });
  }

  function openDuplicates() {
    setDuplicatesOpen(true);
    refreshDuplicates();
  }

  function reviewMerge(candidateIds: string[]) {
    setDuplicatesOpen(false);
    setMergeIds(candidateIds);
    setMergeOpen(true);
  }

  function afterMerge(result: CandidateMergeResult) {
    const removed = new Set(result.merged_candidate_ids);
    setItems((current) => current.filter((item) => !removed.has(item.id)));
    setSelectedIds(new Set());
    setDuplicates((current) => ({
      ...current,
      items: current.items.filter((group) =>
        group.candidates.every((candidate) => !removed.has(candidate.id)),
      ),
      candidate_count: Math.max(
        0,
        current.candidate_count - result.merged_candidate_ids.length,
      ),
    }));
    router.refresh();
  }

  function runBulk(
    body: Omit<CandidateBulkRequest, "candidate_ids">,
    onSuccess?: () => void,
  ) {
    if (selectedIds.size === 0 || isBulkPending) return;
    startBulk(async () => {
      const result = await applyCandidateBulkAction({
        ...body,
        candidate_ids: [...selectedIds],
      });
      if (!result.ok) {
        toast.error(m.candidates.explorer.bulk.failed(result.error));
        return;
      }
      toast.success(
        m.candidates.explorer.bulk.changed(result.data.changed_candidate_ids.length),
      );
      onSuccess?.();
      setSelectedIds(new Set());
      router.refresh();
    });
  }

  function tagSelected(action: "tag_add" | "tag_remove") {
    const tag = bulkTag.trim();
    if (!tag) return;
    runBulk({ action, tag });
  }

  function moveSelected() {
    if (!bulkPositionId) {
      toast.error(m.candidates.explorer.bulk.needCommonPosition);
      return;
    }
    runBulk({
      action: "stage",
      position_id: bulkPositionId,
      stage: bulkStage,
    });
  }

  function rejectSelected() {
    if (!bulkPositionId) {
      toast.error(m.candidates.explorer.bulk.needCommonPosition);
      return;
    }
    setBulkRejectOpen(true);
  }

  async function exportSelected() {
    if (selectedIds.size === 0 || isExporting) return;
    setIsExporting(true);
    try {
      const response = await fetch("/api/candidates/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          candidate_ids: [...selectedIds],
          position_id: bulkPositionId || null,
          include_contacts: includeContactsInExport,
        }),
      });
      if (!response.ok) throw new Error(m.candidates.explorer.loadFailed);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const disposition = response.headers.get("content-disposition") ?? "";
      const matchedName = disposition.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i)?.[1];
      link.href = url;
      link.download = matchedName
        ? decodeURIComponent(matchedName.replace(/^\"|\"$/g, ""))
        : "candidates.csv";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setIncludeContactsInExport(false);
    } catch (error) {
      toast.error(
        m.candidates.explorer.bulk.failed((error as Error).message),
      );
    } finally {
      setIsExporting(false);
    }
  }

  return (
    <>
      <section className="mb-5 rounded-xl border border-border bg-bg-elevated p-4">
        <div className="flex flex-wrap items-center gap-2.5">
          <div className="relative min-w-[240px] flex-1">
            <Search className="pointer-events-none absolute left-[11px] top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text-faint" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={m.candidates.explorer.searchHint}
              className={cn(controlClass, "w-full py-2 pl-8 pr-8")}
            />
            {query ? (
              <button
                type="button"
                onClick={() => setQuery("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-text-faint hover:bg-bg-hover hover:text-text"
                aria-label={m.candidates.explorer.clearFilters}
              >
                <X className="h-3 w-3" />
              </button>
            ) : null}
          </div>

          <div className="relative">
            <Bookmark className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text-faint" />
            <select
              value={selectedViewId}
              onChange={(event) => applySavedView(event.target.value)}
              className={cn(controlClass, "min-w-[170px] appearance-none pl-8 pr-8")}
              aria-label={m.candidates.explorer.savedViews}
            >
              <option value="">{m.candidates.explorer.chooseSavedView}</option>
              {savedViews.map((view) => (
                <option key={view.id} value={view.id}>
                  {view.name}
                </option>
              ))}
            </select>
            <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-text-faint" />
          </div>

          {access.writeCandidates ? (
            <button
              type="button"
              onClick={() => setSaveDialogOpen(true)}
              className={cn(controlClass, "inline-flex items-center gap-1.5")}
            >
              <Bookmark className="h-3.5 w-3.5" />
              {m.candidates.explorer.saveView}
            </button>
          ) : null}
          {access.writeCandidates && selectedViewId ? (
            <>
              <button
                type="button"
                disabled={isSavingView}
                onClick={updateSavedView}
                className={controlClass}
              >
                {m.candidates.explorer.updateView}
              </button>
              <button
                type="button"
                disabled={isSavingView}
                onClick={deleteSavedView}
                className="rounded-[6px] px-2.5 py-[7px] text-[12px] text-text-muted hover:bg-score-low/10 hover:text-score-low"
              >
                {m.candidates.explorer.deleteView}
              </button>
            </>
          ) : null}
        </div>

        <div className="mt-4 grid gap-4 border-t border-border pt-4 xl:grid-cols-[1fr_1fr_180px_220px]">
          <FilterGroup label={m.candidates.explorer.sourceLabel}>
            {sourceOptions.length === 0 ? (
              <span className="text-[11px] text-text-faint">—</span>
            ) : (
              sourceOptions.map((source) => (
                <FilterChip
                  key={source}
                  active={filters.sources.includes(source)}
                  onClick={() => toggleArrayFilter("sources", source)}
                >
                  {source}
                </FilterChip>
              ))
            )}
          </FilterGroup>

          <FilterGroup label={m.candidates.explorer.stageLabel}>
            {KANBAN_STAGES.map((stage) => (
              <FilterChip
                key={stage}
                active={filters.stages.includes(stage)}
                onClick={() => toggleArrayFilter("stages", stage)}
              >
                {m.kanban.stages[stage]}
              </FilterChip>
            ))}
          </FilterGroup>

          <FilterGroup label={m.candidates.explorer.scoreLabel}>
            <div className="flex items-center gap-1.5">
              <input
                type="number"
                min={0}
                max={100}
                value={scoreMin}
                onChange={(event) => setScoreMin(event.target.value)}
                onBlur={applyScores}
                onKeyDown={(event) => event.key === "Enter" && applyScores()}
                placeholder={m.candidates.explorer.scoreMin}
                className={cn(controlClass, "min-w-0 w-[74px]")}
              />
              <span className="text-text-faint">–</span>
              <input
                type="number"
                min={0}
                max={100}
                value={scoreMax}
                onChange={(event) => setScoreMax(event.target.value)}
                onBlur={applyScores}
                onKeyDown={(event) => event.key === "Enter" && applyScores()}
                placeholder={m.candidates.explorer.scoreMax}
                className={cn(controlClass, "min-w-0 w-[74px]")}
              />
            </div>
          </FilterGroup>

          <FilterGroup label={m.candidates.explorer.positionLabel}>
            <select
              value={filters.position_id ?? ""}
              onChange={(event) =>
                patchFilters({ position_id: event.target.value || null })
              }
              className={cn(controlClass, "w-full")}
              aria-label={m.candidates.explorer.positionLabel}
            >
              <option value="">{m.candidates.explorer.allPositions}</option>
              {filterPositions.map((position) => (
                <option key={position.id} value={position.id}>
                  {position.title}
                  {position.status === "closed"
                    ? ` · ${m.status.closed}`
                    : ""}
                </option>
              ))}
            </select>
          </FilterGroup>
        </div>

        <div className="mt-4 flex flex-wrap items-end gap-3 border-t border-border pt-4">
          <label className="min-w-[240px] flex-1 text-[10px] font-medium uppercase tracking-[0.07em] text-text-faint">
            {m.candidates.explorer.tagsLabel}
            <div className="relative mt-1.5">
              <Tags className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
              <input
                value={tagFilter}
                onChange={(event) => setTagFilter(event.target.value)}
                onBlur={applyTags}
                onKeyDown={(event) => event.key === "Enter" && applyTags()}
                placeholder={m.candidates.explorer.tagsPlaceholder}
                className={cn(controlClass, "w-full pl-8")}
              />
            </div>
          </label>
          <BooleanFilter
            label={m.candidates.tabFavorites}
            active={filters.favorites_only}
            onClick={() => patchFilters({ favorites_only: !filters.favorites_only })}
          />
          <BooleanFilter
            label={m.candidates.tabNew}
            active={filters.new_only}
            onClick={() => patchFilters({ new_only: !filters.new_only })}
          />
          <BooleanFilter
            label={m.candidates.tabUnscored}
            active={filters.unscored_only}
            onClick={() => patchFilters({ unscored_only: !filters.unscored_only })}
          />
          <button
            type="button"
            onClick={openDuplicates}
            className={cn(controlClass, "inline-flex items-center gap-1.5")}
          >
            <UsersRound className="h-3.5 w-3.5" />
            {m.candidates.explorer.duplicates}
            {duplicates.candidate_count > 0 ? (
              <span className="rounded-full bg-score-mid/15 px-1.5 font-mono text-[10px] text-score-mid">
                {duplicates.candidate_count}
              </span>
            ) : null}
          </button>
          {activeFilterCount > 0 ? (
            <button
              type="button"
              onClick={clearFilters}
              className="ml-auto inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-[7px] text-[12px] text-text-muted hover:bg-bg-hover hover:text-text"
            >
              <X className="h-3.5 w-3.5" />
              {m.candidates.explorer.clearFilters}
              <span className="font-mono text-[10px] text-text-faint">
                {activeFilterCount}
              </span>
            </button>
          ) : null}
        </div>
      </section>

      {initialLoadError ? (
        <div className="rounded-xl border border-score-low/30 bg-score-low/5 p-5 text-[13px] text-score-low">
          {m.candidates.explorer.loadFailed}
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          title={activeFilterCount ? m.candidates.noMatchesTitle : m.candidates.emptyTitle}
          description={
            activeFilterCount ? m.candidates.noMatchesDesc : m.candidates.emptyDesc
          }
        />
      ) : (
        <div className={cn(isNavigating && "opacity-60")}>
          <div className="mb-2 flex items-center justify-between px-1">
            {canSelect ? (
              <label className="inline-flex cursor-pointer items-center gap-2 text-[11px] text-text-muted">
                <input
                  type="checkbox"
                  checked={allVisibleSelected}
                  onChange={toggleAllVisible}
                  className="h-3.5 w-3.5 rounded border-border accent-current"
                />
                {m.candidates.explorer.selectAll}
              </label>
            ) : (
              <span />
            )}
            <span className="font-mono text-[10.5px] text-text-faint">
              {m.candidates.explorer.results(total)}
            </span>
          </div>
          <ul className="overflow-hidden rounded-xl border border-border bg-bg-elevated">
            {items.map((candidate, index) => (
              <CandidateRow
                key={candidate.id}
                candidate={candidate}
                selected={selectedIds.has(candidate.id)}
                onSelect={() => toggleCandidate(candidate.id)}
                selectable={canSelect}
                canWrite={access.writeCandidates}
                onFavoriteChange={(favorite) =>
                  setItems((current) =>
                    current.map((item) =>
                      item.id === candidate.id
                        ? { ...item, is_favorite: favorite }
                        : item,
                    ),
                  )
                }
                selectedPositionId={filters.position_id}
                isLast={index === items.length - 1}
              />
            ))}
          </ul>
          {nextCursor ? (
            <div className="mt-4 flex justify-center">
              <button
                type="button"
                disabled={isLoadingMore}
                onClick={loadMore}
                className="inline-flex items-center gap-2 rounded-[7px] border border-border bg-bg-elevated px-4 py-2 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text disabled:opacity-50"
              >
                {isLoadingMore ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                {isLoadingMore
                  ? m.candidates.explorer.loading
                  : m.candidates.explorer.loadMore}
              </button>
            </div>
          ) : null}
        </div>
      )}

      {selectedIds.size > 0 && canSelect ? (
        <div className="sticky bottom-4 z-30 mx-auto mt-6 flex max-w-[1180px] flex-wrap items-center gap-2 rounded-xl border border-border-strong bg-bg-elevated/95 p-3 shadow-xl backdrop-blur">
          <span className="mr-1 rounded-[6px] bg-bg-active px-2.5 py-1.5 text-[12px] font-medium">
            {m.candidates.explorer.selected(selectedIds.size)}
          </span>
          {access.writeCandidates ? (
            <>
              <input
                value={bulkTag}
                onChange={(event) => setBulkTag(event.target.value)}
                placeholder={m.candidates.explorer.bulk.tagPlaceholder}
                className={cn(controlClass, "w-[120px]")}
              />
              <button type="button" disabled={!bulkTag.trim() || isBulkPending} onClick={() => tagSelected("tag_add")} className={controlClass}>
                {m.candidates.explorer.bulk.addTag}
              </button>
              <button type="button" disabled={!bulkTag.trim() || isBulkPending} onClick={() => tagSelected("tag_remove")} className={controlClass}>
                {m.candidates.explorer.bulk.removeTag}
              </button>
              <span className="mx-1 h-6 w-px bg-border" />
              <select
                value={bulkPositionId}
                onChange={(event) => setBulkPositionId(event.target.value)}
                disabled={commonPositionIds.length === 0}
                className={cn(controlClass, "max-w-[180px]")}
                title={commonPositionIds.length === 0 ? m.candidates.explorer.bulk.needCommonPosition : undefined}
              >
                {commonPositionIds.length === 0 ? <option value="">{m.candidates.explorer.bulk.position}</option> : null}
                {openPositions.filter((position) => commonPositionIds.includes(position.id)).map((position) => (
                  <option key={position.id} value={position.id}>{position.title}</option>
                ))}
              </select>
              <select value={bulkStage} onChange={(event) => setBulkStage(event.target.value as KanbanStage)} className={controlClass}>
                {KANBAN_STAGES.filter((stage) => stage !== "rejected").map((stage) => (
                  <option key={stage} value={stage}>{m.kanban.stages[stage]}</option>
                ))}
              </select>
              <button type="button" disabled={!bulkPositionId || isBulkPending} onClick={moveSelected} className={controlClass}>
                {m.candidates.explorer.bulk.move}
              </button>
              <button type="button" disabled={!bulkPositionId || isBulkPending} onClick={rejectSelected} className="rounded-[6px] border border-score-low/30 px-2.5 py-[7px] text-[12px] text-score-low hover:bg-score-low/10 disabled:opacity-40">
                {m.candidates.explorer.bulk.reject}
              </button>
              <button
                type="button"
                disabled={selectedIds.size < 2 || selectedIds.size > 20}
                onClick={() => {
                  if (selectedIds.size < 2 || selectedIds.size > 20) {
                    toast.error(m.candidates.explorer.bulk.mergeSelectionLimit);
                    return;
                  }
                  setMergeIds([...selectedIds]);
                  setMergeOpen(true);
                }}
                className={cn(controlClass, "inline-flex items-center gap-1.5")}
                title={selectedIds.size < 2 || selectedIds.size > 20 ? m.candidates.explorer.bulk.mergeSelectionLimit : undefined}
              >
                <GitMerge className="h-3.5 w-3.5" />
                {m.candidates.explorer.bulk.merge}
              </button>
              {isBulkPending ? <Loader2 className="h-4 w-4 animate-spin text-text-muted" /> : null}
            </>
          ) : null}
          {access.sendCandidateMessages ? (
            <button
              type="button"
              disabled={
                durableRecipientLimit == null ||
                selectedIds.size > durableRecipientLimit
              }
              onClick={() => {
                if (
                  synchronousRecipientLimit == null ||
                  durableRecipientLimit == null
                ) {
                  toast.error(m.communications.loadFailed);
                  return;
                }
                if (selectedIds.size > durableRecipientLimit) {
                  toast.error(
                    m.communications.bulkLimitExceeded(
                      selectedIds.size,
                      durableRecipientLimit,
                    ),
                  );
                  return;
                }
                setBulkMessageOpen(true);
              }}
              className={cn(
                controlClass,
                "inline-flex items-center gap-1.5 disabled:cursor-not-allowed disabled:opacity-40",
              )}
              title={
                synchronousRecipientLimit == null || durableRecipientLimit == null
                  ? m.communications.loadFailed
                  : selectedIds.size > durableRecipientLimit
                    ? m.communications.bulkLimitExceeded(
                        selectedIds.size,
                        durableRecipientLimit,
                      )
                    : `${m.communications.bulkLimit(synchronousRecipientLimit)} ${m.communications.bulkDurableLimit(durableRecipientLimit)}`
              }
            >
              <Mail className="h-3.5 w-3.5" />
              {m.communications.bulkTitle}
            </button>
          ) : null}
          {access.exportCandidates ? (
            <>
              {access.viewCandidateContacts ? (
                <label className="flex min-h-9 cursor-pointer items-center gap-2 rounded-[6px] border border-border bg-bg-elevated px-2.5 text-[12px] text-text-muted outline-none transition-colors hover:border-border-strong focus-within:ring-2 focus-within:ring-primary/40" title={m.candidates.explorer.bulk.includeContactsHint}>
                  <input type="checkbox" checked={includeContactsInExport} disabled={isExporting} onChange={(event) => setIncludeContactsInExport(event.target.checked)} aria-describedby="candidate-export-contact-hint" className="h-4 w-4 accent-primary" />
                  {m.candidates.explorer.bulk.includeContacts}
                  <span id="candidate-export-contact-hint" className="sr-only">{m.candidates.explorer.bulk.includeContactsHint}</span>
                </label>
              ) : null}
              <button
                type="button"
                disabled={isExporting}
                onClick={exportSelected}
                className={cn(controlClass, "inline-flex items-center gap-1.5")}
                title={includeContactsInExport ? m.candidates.explorer.bulk.exportWithContactsHint : m.candidates.explorer.bulk.exportRedactedHint}
              >
                {isExporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                {isExporting ? m.candidates.explorer.bulk.exporting : m.candidates.explorer.bulk.exportCsv}
              </button>
            </>
          ) : null}
          <button
            type="button"
            onClick={() => {
              setSelectedIds(new Set());
              setIncludeContactsInExport(false);
            }}
            className="ml-auto rounded-[6px] p-2 text-text-faint hover:bg-bg-hover hover:text-text"
            title={m.candidates.explorer.bulk.clear}
            aria-label={m.candidates.explorer.bulk.clear}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      ) : null}

      {access.writeCandidates ? <Dialog open={saveDialogOpen} onOpenChange={setSaveDialogOpen}>
        <DialogContent className="sm:max-w-[420px]">
          <DialogHeader>
            <DialogTitle>{m.candidates.explorer.saveViewTitle}</DialogTitle>
          </DialogHeader>
          <label className="text-[11px] font-medium text-text-muted">
            {m.candidates.explorer.viewName}
            <input
              autoFocus
              value={viewName}
              onChange={(event) => setViewName(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && createSavedView()}
              placeholder={m.candidates.explorer.viewNamePlaceholder}
              className={cn(controlClass, "mt-1.5 w-full")}
            />
          </label>
          <DialogFooter className="mt-2 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => setSaveDialogOpen(false)}
              className="rounded-[6px] px-3 py-1.5 text-[12px] text-text-muted hover:bg-bg-hover"
            >
              {m.common.cancel}
            </button>
            <button
              type="button"
              disabled={!viewName.trim() || isSavingView}
              onClick={createSavedView}
              className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg disabled:opacity-40"
            >
              {isSavingView ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              {m.candidates.explorer.createView}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog> : null}

      <DuplicatesDialog
        open={duplicatesOpen}
        onOpenChange={setDuplicatesOpen}
        page={duplicates}
        loading={isLoadingDuplicates}
        loadFailed={duplicatesLoadFailed}
        onRetry={refreshDuplicates}
        onReview={reviewMerge}
        canMerge={access.writeCandidates}
      />
      {access.writeCandidates ? (
        <CandidateMergeDialog
          open={mergeOpen}
          candidateIds={mergeIds}
          onOpenChange={setMergeOpen}
          onMerged={afterMerge}
        />
      ) : null}
      {access.writeCandidates ? (
        <DispositionDialog
          open={bulkRejectOpen}
          count={selectedIds.size}
          busy={isBulkPending}
          onOpenChange={setBulkRejectOpen}
          onConfirm={(disposition, reason) => {
            runBulk(
              {
                action: "reject",
                position_id: bulkPositionId,
                disposition_code: disposition,
                reason,
              },
              () => setBulkRejectOpen(false),
            );
          }}
        />
      ) : null}
      {access.sendCandidateMessages ? (
        <BulkMessageDialog
          open={bulkMessageOpen}
          onOpenChange={setBulkMessageOpen}
          candidates={selectedItems}
          positions={positions}
          synchronousRecipientLimit={synchronousRecipientLimit}
          durableRecipientLimit={durableRecipientLimit}
          onSent={() => {
            setSelectedIds(new Set());
            router.refresh();
          }}
        />
      ) : null}
    </>
  );
}

function FilterGroup({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <fieldset>
      <legend className="mb-1.5 text-[10px] font-medium uppercase tracking-[0.07em] text-text-faint">
        {label}
      </legend>
      <div className="flex flex-wrap gap-1.5">{children}</div>
    </fieldset>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded-[6px] border px-2 py-1 text-[10.5px] transition-colors",
        active
          ? "border-border-strong bg-bg-active text-text"
          : "border-border bg-bg text-text-muted hover:bg-bg-hover hover:text-text",
      )}
    >
      {children}
    </button>
  );
}

function BooleanFilter({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return <FilterChip active={active} onClick={onClick}>{label}</FilterChip>;
}

function CandidateRow({
  candidate,
  selected,
  onSelect,
  selectable,
  canWrite,
  onFavoriteChange,
  selectedPositionId,
  isLast,
}: {
  candidate: CandidateSearchItem;
  selected: boolean;
  onSelect: () => void;
  selectable: boolean;
  canWrite: boolean;
  onFavoriteChange: (favorite: boolean) => void;
  selectedPositionId: string | null;
  isLast: boolean;
}) {
  const { m } = useI18n();
  const publicSourceRef =
    candidate.source === "identity_review" ? null : candidate.source_ref;
  const ref = candRef(candidate.id, publicSourceRef);
  const displayRef = candDisplayRef(candidate.id, publicSourceRef);
  const viewed = candidate.viewed_at != null;
  const href = selectedPositionId
    ? `/candidates/${candidate.id}?position=${encodeURIComponent(selectedPositionId)}`
    : `/candidates/${candidate.id}`;

  return (
    <li
      className={cn(
        "group flex items-center transition-colors hover:bg-bg-hover",
        selected && "bg-bg-active",
        !isLast && "border-b border-border",
      )}
    >
      {selectable ? (
        <label className="flex self-stretch cursor-pointer items-center pl-4 pr-1">
          <input
            type="checkbox"
            checked={selected}
            onChange={onSelect}
            aria-label={m.candidates.explorer.selectCandidate(displayRef)}
            className="h-3.5 w-3.5 rounded border-border accent-current"
          />
        </label>
      ) : null}
      <Link
        href={href}
        className="flex min-w-0 flex-1 items-center gap-4 px-3 py-4"
      >
        <ScoreChip score={candidate.best_score} size="sm" />
        <div className="flex min-w-0 flex-[1.2] items-center gap-2.5">
          <SourceBadge source={candidate.source} />
          <span
            className="truncate font-mono text-[12px] font-medium text-text"
            title={ref}
          >
            {displayRef}
          </span>
          {viewed ? (
            <span
              title={m.position.viewedAgo(m.format.relativeTime(candidate.viewed_at!))}
              className="flex items-center text-text-faint"
            >
              <Eye className="h-3 w-3" />
            </span>
          ) : null}
          {candidate.duplicate_count > 0 ? (
            <span className="rounded-full border border-score-mid/30 bg-score-mid/10 px-1.5 py-0.5 text-[9.5px] text-score-mid">
              {m.candidates.explorer.duplicateBadge(candidate.duplicate_count)}
            </span>
          ) : null}
        </div>
        <div className="hidden min-w-0 flex-[1.35] flex-col gap-1 sm:flex">
          {candidate.best_position_id && candidate.best_position_title ? (
            <>
              <span className="truncate text-[12px] text-text-muted group-hover:text-text">
                {candidate.best_position_title}
              </span>
              <div className="flex flex-wrap gap-1">
                {candidate.stages.slice(0, 3).map((stage) => (
                  <span
                    key={stage}
                    className="rounded-[4px] border border-border bg-bg px-1.5 py-0.5 text-[9.5px] text-text-muted"
                  >
                    {m.kanban.stages[stage]}
                  </span>
                ))}
              </div>
            </>
          ) : (
            <span className="text-[12px] italic text-text-faint">
              {m.candidates.notScored}
            </span>
          )}
        </div>
        <div className="hidden min-w-0 flex-1 flex-wrap gap-1 lg:flex">
          {candidate.tags.slice(0, 4).map((tag) => (
            <span
              key={tag}
              className="max-w-[110px] truncate rounded-full border border-border bg-bg px-2 py-0.5 text-[9.5px] text-text-muted"
            >
              {tag}
            </span>
          ))}
          {candidate.tags.length > 4 ? (
            <span className="text-[10px] text-text-faint">+{candidate.tags.length - 4}</span>
          ) : null}
        </div>
        <span className="hidden w-[90px] shrink-0 text-right font-mono text-[10px] text-text-muted md:inline">
          {m.format.relativeTime(candidate.created_at)}
        </span>
        <ArrowRight className="h-3.5 w-3.5 shrink-0 text-text-faint opacity-0 transition-opacity group-hover:opacity-100" />
      </Link>
      {canWrite ? (
        <div className="flex items-center gap-0.5 pr-3">
          <FavoriteButton
            candidateId={candidate.id}
            initial={candidate.is_favorite}
            parentPath="/candidates"
            onChange={onFavoriteChange}
          />
          <DeleteCandidateButton
            candidateId={candidate.id}
            candidateLabel={ref}
            variant="icon"
          />
        </div>
      ) : null}
    </li>
  );
}

function DuplicatesDialog({
  open,
  onOpenChange,
  page,
  loading,
  loadFailed,
  onRetry,
  onReview,
  canMerge,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  page: CandidateDuplicatePage;
  loading: boolean;
  loadFailed: boolean;
  onRetry: () => void;
  onReview: (candidateIds: string[]) => void;
  canMerge: boolean;
}) {
  const { m } = useI18n();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[680px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-[15px]">
            <UsersRound className="h-4 w-4 text-text-muted" />
            {m.candidates.explorer.duplicatesTitle}
          </DialogTitle>
          <DialogDescription className="text-[12px] text-text-muted">
            {m.candidates.explorer.duplicatesDesc}
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[64vh] space-y-3 overflow-y-auto pr-1">
          {loadFailed ? (
            <div className="rounded-lg border border-score-low/30 bg-score-low/5 px-4 py-3">
              <p role="alert" className="text-[12px] text-score-low">
                {m.candidates.explorer.duplicatesLoadFailed}
              </p>
              <button
                type="button"
                disabled={loading}
                onClick={onRetry}
                className="mt-2 inline-flex items-center gap-1.5 rounded-[6px] border border-score-low/30 bg-bg-elevated px-2.5 py-1.5 text-[11px] font-medium text-score-low transition-colors hover:bg-score-low/10 disabled:opacity-50"
              >
                {loading ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : null}
                {m.candidates.explorer.retryDuplicates}
              </button>
            </div>
          ) : loading && page.items.length === 0 ? (
            <div className="flex items-center justify-center gap-2 py-12 text-[12px] text-text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              {m.candidates.explorer.loading}
            </div>
          ) : page.items.length === 0 ? (
            <p className="py-10 text-center text-[12px] text-text-muted">
              {m.candidates.explorer.noDuplicates}
            </p>
          ) : (
            page.items.map((group) => (
              <DuplicateGroupCard key={group.id} group={group} onReview={onReview} canMerge={canMerge} />
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function DuplicateGroupCard({
  group,
  onReview,
  canMerge,
}: {
  group: CandidateDuplicateGroup;
  onReview: (candidateIds: string[]) => void;
  canMerge: boolean;
}) {
  const { m } = useI18n();
  return (
    <article className="rounded-lg border border-border bg-bg p-3">
      <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-1">
          {group.match_types.map((matchType) => (
            <span
              key={matchType}
              className="rounded-full bg-score-mid/10 px-2 py-0.5 text-[10px] text-score-mid"
            >
              {m.candidates.explorer.matchTypes[matchType]}
            </span>
          ))}
        </div>
        {canMerge ? (
          <button
            type="button"
            onClick={() => onReview(group.candidates.map((candidate) => candidate.id))}
            className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-2.5 py-1.5 text-[11px] font-medium text-text-muted hover:bg-bg-hover hover:text-text"
          >
            <GitMerge className="h-3.5 w-3.5" />
            {m.candidates.explorer.review}
          </button>
        ) : null}
      </div>
      {group.has_more_candidates ? (
        <p role="status" className="mb-2.5 text-[11px] text-text-muted">
          {m.candidates.explorer.duplicateMembersShown(
            group.candidates.length,
            group.candidate_count,
          )}
        </p>
      ) : null}
      <div className="grid gap-2 sm:grid-cols-2">
        {group.candidates.map((candidate) => (
          <Link
            key={candidate.id}
            href={`/candidates/${candidate.id}`}
            className="flex min-w-0 items-center gap-2 rounded-[6px] border border-border bg-bg-elevated p-2.5 hover:bg-bg-hover"
          >
            <SourceBadge source={candidate.source} />
            <span className="min-w-0 flex-1 truncate font-mono text-[10.5px]">
              {candDisplayRef(
                candidate.id,
                candidate.source === "identity_review"
                  ? null
                  : candidate.source_ref,
              )}
            </span>
            <ScoreChip score={candidate.best_score} size="sm" />
          </Link>
        ))}
      </div>
    </article>
  );
}
