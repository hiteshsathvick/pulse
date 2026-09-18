import { apiFetch, toApiError } from "./api";

export type User = {
  id: string;
  email: string;
  name: string;
  is_active: boolean;
};

export type TokenPair = {
  access_token: string;
  refresh_token: string;
  token_type: string;
};

export async function registerUser(email: string, password: string, name: string): Promise<User> {
  const response = await apiFetch("/api/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, name }),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function login(email: string, password: string): Promise<TokenPair> {
  const response = await apiFetch("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function refresh(refreshToken: string): Promise<TokenPair> {
  const response = await apiFetch("/api/v1/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function logout(refreshToken: string): Promise<void> {
  await apiFetch("/api/v1/auth/logout", {
    method: "POST",
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
}

export async function fetchMe(accessToken: string): Promise<User> {
  const response = await apiFetch("/api/v1/auth/me", {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}
