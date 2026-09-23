import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as billingApi from "@/lib/billing-api";
import { MockCheckoutPage } from "./MockCheckoutPage";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: vi.fn() }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));
vi.mock("@/lib/billing-api", () => ({
  getSubscription: vi.fn(),
  mockSubscribe: vi.fn(),
  mockCancel: vi.fn(),
}));

const getSubscription = vi.mocked(billingApi.getSubscription);
const mockSubscribe = vi.mocked(billingApi.mockSubscribe);
const mockCancel = vi.mocked(billingApi.mockCancel);

const FREE_SUBSCRIPTION: billingApi.Subscription = {
  plan: "free",
  status: "active",
  quota_events_per_month: 10_000,
  current_period_start: null,
  current_period_end: null,
};

const PRO_SUBSCRIPTION: billingApi.Subscription = {
  plan: "pro",
  status: "active",
  quota_events_per_month: 1_000_000,
  current_period_start: "2026-09-01T00:00:00Z",
  current_period_end: "2026-10-01T00:00:00Z",
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MockCheckoutPage orgId="org-1" />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("MockCheckoutPage", () => {
  it("a free org sees an Upgrade flow with a Confirm subscription button", async () => {
    getSubscription.mockResolvedValue(FREE_SUBSCRIPTION);
    renderPage();

    expect(await screen.findByText("Upgrade to Pro")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm subscription" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel subscription" })).not.toBeInTheDocument();
  });

  it("a pro org sees a Manage flow with a Cancel subscription button", async () => {
    getSubscription.mockResolvedValue(PRO_SUBSCRIPTION);
    renderPage();

    expect(await screen.findByText("Manage subscription")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel subscription" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm subscription" })).not.toBeInTheDocument();
  });

  it("confirming subscribes then navigates back to the billing page", async () => {
    getSubscription.mockResolvedValue(FREE_SUBSCRIPTION);
    mockSubscribe.mockResolvedValue({ plan: "pro", status: "active" });
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Confirm subscription" }));

    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledWith("token", "org-1"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/orgs/org-1/billing"));
  });

  it("cancelling calls mockCancel then navigates back to the billing page", async () => {
    getSubscription.mockResolvedValue(PRO_SUBSCRIPTION);
    mockCancel.mockResolvedValue({ plan: "free", status: "canceled" });
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Cancel subscription" }));

    await waitFor(() => expect(mockCancel).toHaveBeenCalledWith("token", "org-1"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/orgs/org-1/billing"));
  });

  it("shows an error message without crashing if the action fails", async () => {
    getSubscription.mockResolvedValue(FREE_SUBSCRIPTION);
    mockSubscribe.mockRejectedValue(new Error("something went wrong"));
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Confirm subscription" }));

    expect(await screen.findByText("something went wrong")).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });
});
