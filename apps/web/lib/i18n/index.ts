"use server";

import { cookies } from "next/headers";
import { revalidatePath } from "next/cache";

import { en } from "@/lib/i18n/messages-en";
import { ru } from "@/lib/i18n/messages-ru";
import {
  DEFAULT_LOCALE,
  LOCALES,
  type Locale,
  type Messages,
} from "@/lib/i18n/types";

const CATALOG: Record<Locale, Messages> = { ru, en };

export async function getLocale(): Promise<Locale> {
  const c = await cookies();
  const v = c.get("locale")?.value;
  if (v && (LOCALES as string[]).includes(v)) return v as Locale;
  return DEFAULT_LOCALE;
}

export async function getMessages(): Promise<Messages> {
  const locale = await getLocale();
  return CATALOG[locale];
}

export async function setLocaleAction(next: Locale): Promise<void> {
  if (!(LOCALES as string[]).includes(next)) return;
  const c = await cookies();
  c.set("locale", next, {
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
    sameSite: "lax",
  });
  revalidatePath("/", "layout");
}
