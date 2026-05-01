import path from "node:path";

// Resolve the project root from the Next.js working directory. Avoids the
// fragile `process.cwd().replace("/web", "")` pattern that strips the wrong
// slice if the repo path itself contains `/web` (e.g.
// `~/dev/webflow-projects/granite/web`). Next.js cwd is always the `web/`
// dir; the project root is one level up.
export function getProjectRoot(): string {
  return path.resolve(process.cwd(), "..");
}

export function getGraniteBinary(): string {
  return path.join(getProjectRoot(), ".venv/bin/granite");
}
