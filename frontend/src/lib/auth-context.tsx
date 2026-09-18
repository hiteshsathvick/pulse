"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import {
  fetchMe,
  login as apiLogin,
  logout as apiLogout,
  refresh as apiRefresh,
  registerUser as apiRegister,
  type User,
} from "./auth-api";
import {
  clearStoredRefreshToken,
  getStoredRefreshToken,
  setStoredRefreshToken,
} from "./refresh-token-storage";

type AuthContextValue = {
  user: User | null;
  accessToken: string | null;
  initializing: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, name: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [initializing, setInitializing] = useState(true);

  useEffect(() => {
    // Silent session recovery on load: the access token lives only in
    // memory, so a full page reload always starts with none. A refresh
    // token persisted in localStorage (see refresh-token-storage.ts) is
    // what lets this recover a session without the user re-entering a
    // password.
    const storedRefreshToken = getStoredRefreshToken();
    // No synchronous setState in the effect body itself (react-hooks'
    // set-state-in-effect rule) -- both branches resolve through the same
    // promise chain, so setInitializing(false) always happens inside
    // .finally(), never as a direct call in the effect body.
    const recovery = storedRefreshToken ? apiRefresh(storedRefreshToken) : Promise.resolve(null);

    recovery
      .then(async (pair) => {
        if (pair === null) {
          setAccessToken(null);
          setUser(null);
          return;
        }
        setStoredRefreshToken(pair.refresh_token);
        setAccessToken(pair.access_token);
        setUser(await fetchMe(pair.access_token));
      })
      .catch(() => {
        clearStoredRefreshToken();
        setAccessToken(null);
        setUser(null);
      })
      .finally(() => setInitializing(false));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const pair = await apiLogin(email, password);
    setStoredRefreshToken(pair.refresh_token);
    setAccessToken(pair.access_token);
    setUser(await fetchMe(pair.access_token));
  }, []);

  const register = useCallback(async (email: string, password: string, name: string) => {
    await apiRegister(email, password, name);
  }, []);

  const logout = useCallback(async () => {
    const storedRefreshToken = getStoredRefreshToken();
    if (storedRefreshToken) {
      await apiLogout(storedRefreshToken);
    }
    clearStoredRefreshToken();
    setAccessToken(null);
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, accessToken, initializing, login, register, logout }),
    [user, accessToken, initializing, login, register, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
