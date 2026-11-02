"use server";

import { apiGetAll } from "@/lib/api-client";
import type { CandidateSummary, Position } from "@/lib/types";

/** @ui-only Command-palette position projection. */
export type PalettePosition = {
  id: string;
  title: string;
  status: string;
  folder: string | null;
  candidateCount: number;
};

/** @ui-only Command-palette candidate projection. */
export type PaletteCandidate = {
  id: string;
  label: string;
  bestPositionTitle: string | null;
  bestScore: number | null;
  isFavorite: boolean;
};

/** @ui-only Command-palette presentation payload. */
export type PaletteData = {
  positions: PalettePosition[];
  candidates: PaletteCandidate[];
};

function candidateLabel(c: CandidateSummary): string {
  if (c.source_ref && c.source_ref.trim()) return c.source_ref;
  return `${c.source} · ${c.id.slice(0, 8)}`;
}

export async function fetchCommandPaletteData(): Promise<PaletteData> {
  try {
    const [positions, candidates] = await Promise.all([
      apiGetAll<Position>("/positions"),
      apiGetAll<CandidateSummary>("/candidates"),
    ]);
    return {
      positions: positions.map((p) => ({
        id: p.id,
        title: p.title,
        status: p.status,
        folder: p.folder,
        candidateCount: p.candidate_count,
      })),
      candidates: candidates.map((c) => ({
        id: c.id,
        label: candidateLabel(c),
        bestPositionTitle: c.best_position_title,
        bestScore: c.best_score,
        isFavorite: c.is_favorite,
      })),
    };
  } catch {
    return { positions: [], candidates: [] };
  }
}
