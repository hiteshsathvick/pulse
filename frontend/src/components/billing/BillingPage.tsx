"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import {
  createCheckoutSession,
  createPortalSession,
  getSubscription,
  getUsage,
  listInvoices,
} from "@/lib/billing-api";

const PLAN_LABEL = { free: "Free", pro: "Pro" } as const;

// The backend's one fixed 503 message when Stripe isn't connected yet
// (pulse/api/billing.py's _not_configured()) -- matched here so the UI can
// show a friendly "not set up yet" state instead of a raw error.
function isNotConfigured(error: unknown): boolean {
  return error instanceof Error && error.message.includes("not configured");
}

export function BillingPage({ orgId }: { orgId: string }) {
  const { accessToken } = useAuth();

  const subscriptionQuery = useQuery({
    queryKey: ["billing-subscription", orgId],
    queryFn: () => getSubscription(accessToken!, orgId),
    enabled: !!accessToken,
  });
  const usageQuery = useQuery({
    queryKey: ["billing-usage", orgId],
    queryFn: () => getUsage(accessToken!, orgId),
    enabled: !!accessToken,
  });
  const invoicesQuery = useQuery({
    queryKey: ["billing-invoices", orgId],
    queryFn: () => listInvoices(accessToken!, orgId),
    enabled: !!accessToken,
  });

  // Both redirect the whole page to a Stripe-hosted URL -- card details and
  // plan changes happen entirely on Stripe's own page, never in Pulse's UI.
  const checkoutMutation = useMutation({
    mutationFn: () => createCheckoutSession(accessToken!, orgId),
    onSuccess: (result) => {
      window.location.href = result.url;
    },
  });
  const portalMutation = useMutation({
    mutationFn: () => createPortalSession(accessToken!, orgId),
    onSuccess: (result) => {
      window.location.href = result.url;
    },
  });

  if (subscriptionQuery.isLoading || usageQuery.isLoading) return <Spinner />;
  if (subscriptionQuery.isError) return <Alert>Failed to load billing information.</Alert>;
  if (!subscriptionQuery.data || !usageQuery.data) return null;

  const subscription = subscriptionQuery.data;
  const usage = usageQuery.data;
  const usagePct = usage.quota_events_per_month
    ? Math.min(100, (usage.events_ingested / usage.quota_events_per_month) * 100)
    : 0;

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-8">
      <h1 className="text-2xl font-semibold">Billing</h1>

      <div className="rounded border p-4">
        <p className="font-medium">{PLAN_LABEL[subscription.plan]} plan</p>
        <p className="text-sm text-gray-500">
          Status: {subscription.status}
          {subscription.current_period_end &&
            ` · renews ${new Date(subscription.current_period_end).toLocaleDateString()}`}
        </p>
      </div>

      <div className="rounded border p-4">
        <p className="font-medium">
          {usage.events_ingested.toLocaleString()} /{" "}
          {usage.quota_events_per_month.toLocaleString()} events this month
        </p>
        <div className="mt-2 h-2 w-full rounded bg-gray-200">
          <div className="h-2 rounded bg-black" style={{ width: `${usagePct}%` }} />
        </div>
        <p className="mt-2 text-sm text-gray-500">
          {usage.mtu.toLocaleString()} monthly tracked users
        </p>
      </div>

      <div className="flex gap-2">
        {subscription.plan === "free" && (
          <Button onClick={() => checkoutMutation.mutate()} disabled={checkoutMutation.isPending}>
            Upgrade to Pro
          </Button>
        )}
        <Button
          variant="secondary"
          onClick={() => portalMutation.mutate()}
          disabled={portalMutation.isPending}
        >
          Manage billing
        </Button>
      </div>
      {checkoutMutation.isError && (
        <Alert>
          {isNotConfigured(checkoutMutation.error)
            ? "Billing isn't configured on this server yet."
            : checkoutMutation.error.message}
        </Alert>
      )}
      {portalMutation.isError && (
        <Alert>
          {isNotConfigured(portalMutation.error)
            ? "Billing isn't configured on this server yet."
            : portalMutation.error.message}
        </Alert>
      )}

      <div>
        <h2 className="mb-2 font-medium">Invoices</h2>
        {invoicesQuery.isLoading && <Spinner />}
        {invoicesQuery.isError && !isNotConfigured(invoicesQuery.error) && (
          <Alert>Failed to load invoices.</Alert>
        )}
        {invoicesQuery.isError && isNotConfigured(invoicesQuery.error) && (
          <p className="text-gray-500">Billing isn&apos;t configured on this server yet.</p>
        )}
        {invoicesQuery.data?.length === 0 && <p className="text-gray-500">No invoices yet.</p>}
        <ul className="flex flex-col gap-2">
          {invoicesQuery.data?.map((invoice) => (
            <li key={invoice.id} className="flex items-center justify-between rounded border p-3">
              <span>
                {new Date(invoice.created * 1000).toLocaleDateString()} ·{" "}
                {(invoice.amount_due / 100).toFixed(2)} {invoice.currency.toUpperCase()} ·{" "}
                {invoice.status}
              </span>
              {invoice.hosted_invoice_url && (
                <a
                  href={invoice.hosted_invoice_url}
                  target="_blank"
                  rel="noreferrer"
                  className="underline"
                >
                  View
                </a>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
