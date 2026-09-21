import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "./auth-context";
import { getStoredRefreshToken, setStoredRefreshToken } from "./refresh-token-storage";

const refreshMock = vi.fn();
const fetchMeMock = vi.fn();
vi.mock("@/lib/auth-api", () => ({
  refresh: (token: string) => refreshMock(token),
  fetchMe: (accessToken: string) => fetchMeMock(accessToken),
  login: vi.fn(),
  logout: vi.fn(),
  registerUser: vi.fn(),
}));

function Probe() {
  const { user, accessToken, initializing } = useAuth();
  return (
    <p data-testid="state">
      {initializing ? "initializing" : `${user?.email ?? "no-user"}|${accessToken ?? "no-token"}`}
    </p>
  );
}

function renderUnderStrictMode() {
  return render(
    <StrictMode>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </StrictMode>
  );
}

// Mirrors the real backend: a refresh token is single-use, so redeeming the
// same one twice makes the second request fail with a 401.
function mockSingleUseRefresh(validToken: string) {
  let current = validToken;
  refreshMock.mockImplementation(async (token: string) => {
    if (token !== current) throw new Error("401 refresh token already used");
    current = "rotated-refresh-token";
    return { access_token: "new-access-token", refresh_token: current, token_type: "bearer" };
  });
}

beforeEach(() => {
  localStorage.clear();
  refreshMock.mockReset();
  fetchMeMock.mockReset();
  fetchMeMock.mockResolvedValue({
    id: "u1",
    email: "user@example.com",
    name: "User",
    is_active: true,
  });
});

describe("AuthProvider session recovery", () => {
  it("recovers the session with a single refresh call under React.StrictMode", async () => {
    setStoredRefreshToken("stored-refresh-token");
    mockSingleUseRefresh("stored-refresh-token");

    renderUnderStrictMode();

    expect(await screen.findByText("user@example.com|new-access-token")).toBeInTheDocument();
    // Strict Mode runs the effect twice in dev; only one request may spend the
    // single-use token, or the second one 401s and wipes the session.
    expect(refreshMock).toHaveBeenCalledTimes(1);
    expect(refreshMock).toHaveBeenCalledWith("stored-refresh-token");
    expect(fetchMeMock).toHaveBeenCalledTimes(1);
    expect(fetchMeMock).toHaveBeenCalledWith("new-access-token");
    expect(getStoredRefreshToken()).toBe("rotated-refresh-token");
  });

  it("clears the stored token and ends signed out when the refresh is rejected", async () => {
    setStoredRefreshToken("expired-refresh-token");
    refreshMock.mockRejectedValue(new Error("401"));

    renderUnderStrictMode();

    expect(await screen.findByText("no-user|no-token")).toBeInTheDocument();
    expect(refreshMock).toHaveBeenCalledTimes(1);
    expect(getStoredRefreshToken()).toBeNull();
  });

  it("skips the refresh call entirely when no token is stored", async () => {
    renderUnderStrictMode();

    expect(await screen.findByText("no-user|no-token")).toBeInTheDocument();
    expect(refreshMock).not.toHaveBeenCalled();
  });

  it("does not reuse a finished recovery for a later, separate mount", async () => {
    setStoredRefreshToken("first-refresh-token");
    mockSingleUseRefresh("first-refresh-token");
    const first = renderUnderStrictMode();
    await screen.findByText("user@example.com|new-access-token");
    first.unmount();

    setStoredRefreshToken("second-refresh-token");
    mockSingleUseRefresh("second-refresh-token");
    renderUnderStrictMode();

    expect(await screen.findByText("user@example.com|new-access-token")).toBeInTheDocument();
    expect(refreshMock).toHaveBeenCalledTimes(2);
    expect(refreshMock).toHaveBeenLastCalledWith("second-refresh-token");
  });
});
