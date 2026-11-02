import { en } from "@/lib/i18n/messages-en";
import { ru } from "@/lib/i18n/messages-ru";
import type { Locale, Messages } from "@/lib/i18n/types";

/**
 * Plain (non-server-action) catalog access — usable from client components
 * inside the LocaleProvider, where the locale is already known on the client.
 */
export const CATALOG: Record<Locale, Messages> = { ru, en };

export function getMessagesFor(locale: Locale): Messages {
  return CATALOG[locale];
}
