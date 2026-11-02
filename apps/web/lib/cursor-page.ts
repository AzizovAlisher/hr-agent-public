/** @local-only Compatibility shape accepted by collection view helpers. */
export type CursorPage<T> = {
  items: T[];
  next_cursor: string | null;
};

export function collectionItems<T>(value: CursorPage<T> | T[]): T[] {
  return Array.isArray(value) ? value : value.items;
}
