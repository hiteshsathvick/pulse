import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProtectedRoute } from "./ProtectedRoute";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

const useAuthMock = vi.fn();
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => useAuthMock(),
}));

beforeEach(() => {
  replace.mockClear();
  useAuthMock.mockReset();
});

describe("ProtectedRoute", () => {
  it("shows a loading state while the session is still initializing", () => {
    useAuthMock.mockReturnValue({ user: null, initializing: true });
    render(
      <ProtectedRoute>
        <p>secret</p>
      </ProtectedRoute>
    );
    expect(screen.getByText("Checking session…")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("redirects to /login when there is no signed-in user", () => {
    useAuthMock.mockReturnValue({ user: null, initializing: false });
    render(
      <ProtectedRoute>
        <p>secret</p>
      </ProtectedRoute>
    );
    expect(replace).toHaveBeenCalledWith("/login");
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("renders children when a user is signed in", () => {
    useAuthMock.mockReturnValue({ user: { id: "u1" }, initializing: false });
    render(
      <ProtectedRoute>
        <p>secret</p>
      </ProtectedRoute>
    );
    expect(screen.getByText("secret")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });
});
