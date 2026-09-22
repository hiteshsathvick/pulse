import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as billingApi from "@/lib/billing-api";
import { BillingPage } from "./BillingPage";

vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));
vi.mock("@/lib/billing-api", () => ({
  getSubscription: vi.fn(),
  getUsage: vi.fn(),
  listInvoices: vi.fn(),
  createCheckoutSession: vi.fn(),
  createPortalSession: vi.fn(),
}));

const getSubscription = vi.mocked(billingApi.getSubscription);
const getUsage = vi.mocked(billingApi.getUsage);
const listInvoices = vi.mocked(billingApi.listInvoices);
const createCheckoutSession = vi.mocked(billingApi.createCheckoutSession);
const createPortalSession = vi.mocked(billingApi.createPortalSession);

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
      <BillingPage orgId="org-1" />
    </QueryClientProvider>
  );
}

// window.location.href is read-write in jsdom, but assigning to it triggers
// jsdom's "not implemented: navigation" noise -- stub it so redirect tests
// stay quiet.
const locationAssign = vi.fn();
Object.defineProperty(window, "location", {
  value: { ...window.location, set href(url: string) { locationAssign(url); } },
  writable: true,
});

beforeEach(() => {
  vi.clearAllMocks();
  getUsage.mockResolvedValue({
    period: "2026-09-01",
    events_ingested: 100,
    mtu: 10,
    quota_events_per_month: 10_000,
  });
  listInvoices.mockResolvedValue([]);
});

describe("BillingPage", () => {
  it("shows the free plan, usage, and an Upgrade button", async () => {
    getSubscription.mockResolvedValue(FREE_SUBSCRIPTION);
    renderPage();

    expect(await screen.findByText("Free plan")).toBeInTheDocument();
    expect(screen.getByText("100 / 10,000 events this month")).toBeInTheDocument();
    expect(screen.getByText("10 monthly tracked users")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upgrade to Pro" })).toBeInTheDocument();
  });

  it("a pro org has no Upgrade button but does show its renewal date", async () => {
    getSubscription.mockResolvedValue(PRO_SUBSCRIPTION);
    renderPage();

    expect(await screen.findByText("Pro plan")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upgrade to Pro" })).not.toBeInTheDocument();
    expect(screen.getByText(/renews/)).toBeInTheDocument();
  });

  it("clicking Upgrade to Pro redirects to the returned Stripe URL", async () => {
    getSubscription.mockResolvedValue(FREE_SUBSCRIPTION);
    createCheckoutSession.mockResolvedValue({ url: "https://checkout.stripe.com/fake" });
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Upgrade to Pro" }));

    await waitFor(() => expect(createCheckoutSession).toHaveBeenCalledWith("token", "org-1"));
    await waitFor(() => expect(locationAssign).toHaveBeenCalledWith("https://checkout.stripe.com/fake"));
  });

  it("clicking Manage billing redirects to the returned portal URL", async () => {
    getSubscription.mockResolvedValue(PRO_SUBSCRIPTION);
    createPortalSession.mockResolvedValue({ url: "https://billing.stripe.com/fake" });
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Manage billing" }));

    await waitFor(() => expect(createPortalSession).toHaveBeenCalledWith("token", "org-1"));
    await waitFor(() => expect(locationAssign).toHaveBeenCalledWith("https://billing.stripe.com/fake"));
  });

  it("shows a friendly message, not a raw error, when billing isn't configured", async () => {
    getSubscription.mockResolvedValue(FREE_SUBSCRIPTION);
    createCheckoutSession.mockRejectedValue(new Error("Billing is not configured on this server yet"));
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Upgrade to Pro" }));

    expect(await screen.findByText("Billing isn't configured on this server yet.")).toBeInTheDocument();
  });

  it("lists invoices when there are some", async () => {
    getSubscription.mockResolvedValue(PRO_SUBSCRIPTION);
    listInvoices.mockResolvedValue([
      {
        id: "in_1",
        status: "paid",
        amount_due: 2000,
        currency: "usd",
        hosted_invoice_url: "https://invoice.stripe.com/fake",
        created: 1_700_000_000,
      },
    ]);
    renderPage();

    expect(await screen.findByText(/20.00 USD/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View" })).toHaveAttribute(
      "href",
      "https://invoice.stripe.com/fake"
    );
  });

  it("shows an empty state with no invoices", async () => {
    getSubscription.mockResolvedValue(FREE_SUBSCRIPTION);
    renderPage();
    expect(await screen.findByText("No invoices yet.")).toBeInTheDocument();
  });
});
