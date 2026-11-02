import {
  KANBAN_STAGES,
  type CandidateSearchFilters,
  type KanbanStage,
} from "@/lib/types";

/** @ui-only Candidate-search URL state before generated query serialization. */
export type CandidateSearchParamInput = Record<
  string,
  string | string[] | undefined
>;

export const EMPTY_CANDIDATE_FILTERS: CandidateSearchFilters = {
  q: null,
  score_min: null,
  score_max: null,
  sources: [],
  stages: [],
  position_id: null,
  tags: [],
  favorites_only: false,
  new_only: false,
  unscored_only: false,
};

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function many(value: string | string[] | undefined): string[] {
  if (value == null) return [];
  return (Array.isArray(value) ? value : [value])
    .map((item) => item.trim())
    .filter(Boolean);
}

function boundedScore(value: string | undefined): number | null {
  if (value == null || value.trim() === "") return null;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > 100) return null;
  return parsed;
}

function truthy(value: string | undefined): boolean {
  return value === "1" || value === "true";
}

export function candidateFiltersFromSearchParams(
  params: CandidateSearchParamInput,
): CandidateSearchFilters {
  const q = first(params.q)?.trim() || null;
  const tab = first(params.tab);
  const sources = Array.from(
    new Set(many(params.source).map((source) => source.toLowerCase())),
  ).sort();
  const validStages = new Set<string>(KANBAN_STAGES);
  const stages = Array.from(
    new Set(many(params.stage).filter((stage) => validStages.has(stage))),
  ).sort() as KanbanStage[];
  const tags = Array.from(new Set(many(params.tag))).sort();
  let scoreMin = boundedScore(first(params.score_min));
  let scoreMax = boundedScore(first(params.score_max));
  if (scoreMin != null && scoreMax != null && scoreMin > scoreMax) {
    [scoreMin, scoreMax] = [scoreMax, scoreMin];
  }
  return {
    q,
    score_min: scoreMin,
    score_max: scoreMax,
    sources,
    stages,
    position_id: first(params.position_id)?.trim() || null,
    tags,
    favorites_only:
      truthy(first(params.favorites_only)) ||
      truthy(first(params.favorites)) ||
      tab === "favorites",
    new_only:
      truthy(first(params.new_only)) ||
      truthy(first(params.new)) ||
      tab === "new",
    unscored_only:
      truthy(first(params.unscored_only)) ||
      truthy(first(params.unscored)) ||
      tab === "unscored",
  };
}

export function candidateFiltersToSearchParams(
  filters: CandidateSearchFilters,
  options?: { cursor?: string | null; limit?: number },
): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.score_min != null) {
    params.set("score_min", String(filters.score_min));
  }
  if (filters.score_max != null) {
    params.set("score_max", String(filters.score_max));
  }
  for (const source of filters.sources) params.append("source", source);
  for (const stage of filters.stages) params.append("stage", stage);
  if (filters.position_id) params.set("position_id", filters.position_id);
  for (const tag of filters.tags) params.append("tag", tag);
  if (filters.favorites_only) params.set("favorites_only", "true");
  if (filters.new_only) params.set("new_only", "true");
  if (filters.unscored_only) params.set("unscored_only", "true");
  if (options?.cursor) params.set("cursor", options.cursor);
  if (options?.limit != null) params.set("limit", String(options.limit));
  return params;
}

export function candidateFiltersKey(filters: CandidateSearchFilters): string {
  return JSON.stringify({
    ...filters,
    sources: [...filters.sources].sort(),
    stages: [...filters.stages].sort(),
    tags: [...filters.tags].sort(),
  });
}

export function candidateFilterCount(filters: CandidateSearchFilters): number {
  return (
    (filters.q ? 1 : 0) +
    filters.sources.length +
    filters.stages.length +
    filters.tags.length +
    (filters.score_min != null || filters.score_max != null ? 1 : 0) +
    (filters.position_id ? 1 : 0) +
    (filters.favorites_only ? 1 : 0) +
    (filters.new_only ? 1 : 0) +
    (filters.unscored_only ? 1 : 0)
  );
}
