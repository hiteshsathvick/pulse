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

type RecoveredSession = { accessToken: string; user: User };

// The refresh token is single-use: the backend rotates it on every redeem, so
// spending the same stored token twice makes the second request 401. React
// Strict Mode (on in `next dev`) runs the mount effect twice back to back,
// and both runs read the same not-yet-rotated token from localStorage. An
// effect cleanup / ignore flag can't help -- it would discard the stale
// run's *result*, but the second request would still be sent and still 401.
// So the dedup lives here, at the request: one in-flight recovery per stored
// token, shared by every caller that asks while it is pending. Cleared once
// settled, so a later, separate mount always starts a fresh recovery.
let inFlightRecovery: { token: string; promise: Promise<RecoveredSession | null> } | null = null;

// Never rejects: a failed recovery clears the stored token and resolves null.
function recoverSession(storedRefreshToken: string | null): Promise<RecoveredSession | null> {
  if (storedRefreshToken === null) return Promise.resolve(null);
  if (inFlightRecovery?.token === storedRefreshToken) return inFlightRecovery.promise;

  const promise: Promise<RecoveredSession | null> = apiRefresh(storedRefreshToken)
    .then(async (pair) => {
      setStoredRefreshToken(pair.refresh_token);
      return { accessToken: pair.access_token, user: await fetchMe(pair.access_token) };
    })
    .catch(() => {
      clearStoredRefreshToken();
      return null;
    })
    .finally(() => {
      if (inFlightRecovery?.promise === promise) inFlightRecovery = null;
    });
  inFlightRecovery = { token: storedRefreshToken, promise };
  return promise;
}

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
    //
    // No synchronous setState in the effect body itself (react-hooks'
    // set-state-in-effect rule) -- every branch, including "nothing stored",
    // resolves through this one promise chain, so the setState calls only
    // ever run inside .then()/.finally(). Under Strict Mode both effect runs
    // share one recovery (see recoverSession) and each applies the same
    // result, which is idempotent.
    recoverSession(getStoredRefreshToken())
      .then((recovered) => {
        setAccessToken(recovered?.accessToken ?? null);
        setUser(recovered?.user ?? null);
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
