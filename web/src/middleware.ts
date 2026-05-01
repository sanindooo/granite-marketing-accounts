import { NextRequest, NextResponse } from "next/server";

// Single-user desktop-app threat model: anything that can reach localhost
// (LAN scan, malicious browser tab, DNS rebinding, hostile extension) can hit
// the API without this gate. Two checks, cheap, defense-in-depth:
//
//   1. Origin must equal Host on state-changing methods. Same-origin browser
//      fetches always send Origin; cross-origin tabs and DNS-rebinding setups
//      send a different one. Server-side callers (curl, the CLI test
//      harness) omit Origin entirely — we reject those for unsafe methods so
//      a malicious extension can't strip the header to bypass the check.
//   2. If GRANITE_LOCAL_TOKEN is set, require a matching Bearer token on
//      every /api/** request. Optional so dev / first-run still works without
//      configuration; required in production setup via `granite ops setup`.
//
// GET is left unauthenticated by design — none of the read routes have
// side effects, and the dashboard makes plenty of cross-feature reads we
// don't want to break.

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (!pathname.startsWith("/api/")) {
    return NextResponse.next();
  }

  const method = request.method.toUpperCase();
  const isUnsafe = !SAFE_METHODS.has(method);

  if (isUnsafe) {
    const origin = request.headers.get("origin");
    const host = request.headers.get("host");
    if (!origin || !host) {
      return jsonError(403, "Origin header required for state-changing requests");
    }
    let originHost: string;
    try {
      originHost = new URL(origin).host;
    } catch {
      return jsonError(403, "Malformed Origin header");
    }
    if (originHost !== host) {
      return jsonError(403, "Origin does not match Host");
    }
  }

  const expectedToken =
    process.env.GRANITE_LOCAL_TOKEN ?? process.env.NEXT_PUBLIC_GRANITE_LOCAL_TOKEN;
  if (expectedToken) {
    const auth = request.headers.get("authorization") ?? "";
    if (auth !== `Bearer ${expectedToken}`) {
      return jsonError(401, "Missing or invalid Authorization header");
    }
  }

  return NextResponse.next();
}

function jsonError(status: number, message: string): NextResponse {
  return NextResponse.json({ ok: false, error: message }, { status });
}

export const config = {
  matcher: ["/api/:path*"],
};
