// The browser talks to the API directly over its host-published port, not
// through Next.js's own server -- unlike the server-rendered health check
// on "/", which uses API_INTERNAL_URL (the docker-internal hostname) since
// it runs inside the Next.js container. NEXT_PUBLIC_* is inlined at build
// time, so this only ever needs a client-reachable URL.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
  return fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    },
    cache: "no-store",
  });
}

export async function toApiError(response: Response): Promise<Error> {
  try {
    const body = await response.json();
    return new Error(body?.error?.message ?? `request failed: ${response.status}`);
  } catch {
    return new Error(`request failed: ${response.status}`);
  }
}

export function authedFetch(
  path: string,
  accessToken: string,
  options: RequestInit = {}
): Promise<Response> {
  return apiFetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${accessToken}`, ...(options.headers ?? {}) },
  });
}
