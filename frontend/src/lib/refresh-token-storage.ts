// The access token lives only in memory (see auth-context.tsx) -- pulse's
// /auth/refresh returns the refresh token in the JSON body, not an httpOnly
// cookie, so *something* has to persist it client-side for a page reload to
// recover a session at all. localStorage is the pragmatic choice given that
// backend shape; the trade-off (more XSS-exposed than an httpOnly cookie)
// was confirmed with the user before building this.
const STORAGE_KEY = "pulse.refresh_token";

export function getStoredRefreshToken(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setStoredRefreshToken(token: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, token);
  } catch {
    // Private browsing / blocked storage: the session just won't survive
    // a reload, which is a degraded experience, not a broken one.
  }
}

export function clearStoredRefreshToken(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing to clean up if storage was never reachable.
  }
}
