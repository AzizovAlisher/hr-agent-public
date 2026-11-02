"use server";

import type { PublicShortlist } from "@/lib/types";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function getPublicShortlist(
  token: string,
): Promise<PublicShortlist | null> {
  const res = await fetch(`${API_BASE_URL}/public/shortlists/${token}`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return (await res.json()) as PublicShortlist;
}
