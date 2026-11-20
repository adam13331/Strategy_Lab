const base = "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(base + path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = j.detail || j.error || JSON.stringify(j);
    } catch {}
    throw new Error(msg);
  }
  return r.json();
}

export const api = {
  get: <T>(path: string) => req<T>(path),
  post: <T>(path: string, body?: unknown) =>
    req<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
};

export const fmt = (n: number | null | undefined, digits = 2) =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const pct = (n: number | null | undefined, digits = 2) =>
  n === null || n === undefined ? "—" : `${n.toFixed(digits)}%`;
