import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./AppShell";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useParams: () => ({}),
}));

const logout = vi.fn();
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ logout, accessToken: "token" }),
}));

// The switcher makes its own network calls via TanStack Query -- out of
// scope for AppShell's own test, which only cares that the shell renders
// nav chrome and wires up logout correctly.
vi.mock("@/components/OrgProjectSwitcher", () => ({
  OrgProjectSwitcher: () => <div data-testid="switcher" />,
}));

beforeEach(() => {
  push.mockClear();
  logout.mockClear();
});

describe("AppShell", () => {
  it("renders the nav, the switcher, and the page content", () => {
    render(
      <AppShell>
        <p>page content</p>
      </AppShell>
    );
    expect(screen.getByRole("link", { name: "Pulse" })).toHaveAttribute("href", "/orgs");
    expect(screen.getByTestId("switcher")).toBeInTheDocument();
    expect(screen.getByText("page content")).toBeInTheDocument();
  });

  it("logs out and redirects to /login when Log out is clicked", async () => {
    render(
      <AppShell>
        <p>page content</p>
      </AppShell>
    );
    await userEvent.click(screen.getByRole("button", { name: "Log out" }));
    expect(logout).toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith("/login");
  });
});
