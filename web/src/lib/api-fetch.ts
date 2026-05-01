// Client-side wrapper around `fetch` that attaches the Bearer token when one
// is configured. Server-side same-origin defense lives in middleware.ts; this
// helper exists only so the dashboard can opt-in to defense-in-depth via
// NEXT_PUBLIC_GRANITE_LOCAL_TOKEN without each call site repeating the header
// plumbing.

const TOKEN = process.env.NEXT_PUBLIC_GRANITE_LOCAL_TOKEN;

export function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  if (!TOKEN) {
    return fetch(input, init);
  }
  const headers = new Headers(init?.headers);
  if (!headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${TOKEN}`);
  }
  return fetch(input, { ...init, headers });
}
