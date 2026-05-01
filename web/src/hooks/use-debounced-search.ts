"use client";

import { useEffect, useState } from "react";

// 300 ms is the same delay the column-trim solution doc claimed was already
// in place. Long enough that "Webflow" is one fetch (typed in ~150 ms by a
// fast user) but short enough not to feel laggy after the user pauses.
const DEFAULT_DEBOUNCE_MS = 300;

export function useDebouncedSearch(value: string | null, delayMs = DEFAULT_DEBOUNCE_MS): string | null {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}
