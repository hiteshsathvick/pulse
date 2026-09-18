import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GuestRoute } from "./GuestRoute";

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

describe("GuestRoute", () => {
  it("shows a loading state while the session is still initializing", () => {
    useAuthMock.mockReturnValue({ user: null, initializing: true });
    render(
      <GuestRoute>
        <p>login form</p>
      </GuestRoute>
    );
    expect(screen.getByText("Checking session…")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("redirects to /orgs when a user is already signed in", () => {
    useAuthMock.mockReturnValue({ user: { id: "u1" }, initializing: false });
    render(
      <GuestRoute>
        <p>login form</p>
      </GuestRoute>
    );
    expect(replace).toHaveBeenCalledWith("/orgs");
    expect(screen.queryByText("login form")).not.toBeInTheDocument();
  });

  it("renders children when there is no signed-in user", () => {
    useAuthMock.mockReturnValue({ user: null, initializing: false });
    render(
      <GuestRoute>
        <p>login form</p>
      </GuestRoute>
    );
    expect(screen.getByText("login form")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });
});
