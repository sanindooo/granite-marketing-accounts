export function getCurrentFY(): string {
  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth() + 1;
  if (month >= 3) {
    return `FY-${year}-${(year + 1).toString().slice(-2)}`;
  }
  return `FY-${year - 1}-${year.toString().slice(-2)}`;
}

export function fyBounds(fy: string): { start: string; end: string } {
  const match = fy.match(/^FY-(\d{4})-(\d{2})$/);
  if (!match) {
    throw new Error(`Invalid fiscal year format: ${fy}`);
  }
  const startYear = parseInt(match[1], 10);
  return {
    start: `${startYear}-03-01`,
    end: `${startYear + 1}-02-28`,
  };
}

export function getAvailableFYs(includeAll = false): string[] {
  const current = getCurrentFY();
  const match = current.match(/^FY-(\d{4})-(\d{2})$/);
  if (!match) return includeAll ? ["all", current] : [current];

  const endYear = parseInt(match[1], 10);
  const fys: string[] = includeAll ? ["all"] : [];
  for (let y = endYear; y >= endYear - 3; y--) {
    fys.push(`FY-${y}-${(y + 1).toString().slice(-2)}`);
  }
  return fys;
}

export function fyBoundsOrAll(fy: string): { start: string; end: string } | null {
  if (fy === "all") return null;
  return fyBounds(fy);
}
