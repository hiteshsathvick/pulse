import { authedFetch, toApiError } from "./api";

// Mirrors backend/pulse/models/billing.py + pulse/api/billing.py.
export type SubscriptionPlan = "free" | "pro";
export type SubscriptionStatus = "active" | "past_due" | "canceled" | "incomplete";

export type Subscription = {
  plan: SubscriptionPlan;
  status: SubscriptionStatus;
  quota_events_per_month: number;
  current_period_start: string | null;
  current_period_end: string | null;
};

export type Usage = {
  period: string;
  events_ingested: number;
  mtu: number;
  quota_events_per_month: number;
};

export type Invoice = {
  id: string;
  status: string | null;
  amount_due: number;
  currency: string;
  hosted_invoice_url: string | null;
  created: number;
};

function billingPath(orgId: string): string {
  return `/api/v1/orgs/${orgId}/billing`;
}

export async function getSubscription(accessToken: string, orgId: string): Promise<Subscription> {
  const response = await authedFetch(`${billingPath(orgId)}/subscription`, accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function getUsage(accessToken: string, orgId: string): Promise<Usage> {
  const response = await authedFetch(`${billingPath(orgId)}/usage`, accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function createCheckoutSession(
  accessToken: string,
  orgId: string
): Promise<{ url: string }> {
  const response = await authedFetch(`${billingPath(orgId)}/checkout`, accessToken, {
    method: "POST",
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function createPortalSession(
  accessToken: string,
  orgId: string
): Promise<{ url: string }> {
  const response = await authedFetch(`${billingPath(orgId)}/portal`, accessToken, {
    method: "POST",
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function listInvoices(accessToken: string, orgId: string): Promise<Invoice[]> {
  const response = await authedFetch(`${billingPath(orgId)}/invoices`, accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export type MockAction = { plan: SubscriptionPlan; status: SubscriptionStatus };

// Only meaningful while the server's payment_provider is "mock" (the
// default) -- the local /billing/mock-checkout page calls these to
// simulate what a real payment provider's webhook would eventually apply.
export async function mockSubscribe(accessToken: string, orgId: string): Promise<MockAction> {
  const response = await authedFetch(`${billingPath(orgId)}/mock/subscribe`, accessToken, {
    method: "POST",
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function mockCancel(accessToken: string, orgId: string): Promise<MockAction> {
  const response = await authedFetch(`${billingPath(orgId)}/mock/cancel`, accessToken, {
    method: "POST",
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}
