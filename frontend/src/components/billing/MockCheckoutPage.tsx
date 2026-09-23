"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Alert, Button, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { getSubscription, mockCancel, mockSubscribe } from "@/lib/billing-api";

// Stands in for a real payment provider's hosted checkout/portal page
// (confirmed with the user first: no external account is needed for this
// project) -- "Confirm" calls the same event-application logic a real
// webhook would eventually trigger (pulse/billing/webhooks.py::handle_event).
const MOCK_PRO_PRICE = "$29.00/month (mock -- no real charge)";

export function MockCheckoutPage({ orgId }: { orgId: string }) {
  const { accessToken } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();

  const subscriptionQuery = useQuery({
    queryKey: ["billing-subscription", orgId],
    queryFn: () => getSubscription(accessToken!, orgId),
    enabled: !!accessToken,
  });

  // The billing page's own queries share these same keys -- without
  // invalidating them here, a mutation's redirect back to /billing can land
  // on data this page itself fetched (and cached) moments ago, still inside
  // the client's staleTime window, so it looks like nothing changed.
  const invalidateBillingQueries = () => {
    queryClient.invalidateQueries({ queryKey: ["billing-subscription", orgId] });
    queryClient.invalidateQueries({ queryKey: ["billing-usage", orgId] });
    queryClient.invalidateQueries({ queryKey: ["billing-invoices", orgId] });
  };

  const subscribeMutation = useMutation({
    mutationFn: () => mockSubscribe(accessToken!, orgId),
    onSuccess: () => {
      invalidateBillingQueries();
      router.push(`/orgs/${orgId}/billing`);
    },
  });
  const cancelMutation = useMutation({
    mutationFn: () => mockCancel(accessToken!, orgId),
    onSuccess: () => {
      invalidateBillingQueries();
      router.push(`/orgs/${orgId}/billing`);
    },
  });

  if (subscriptionQuery.isLoading) return <Spinner />;
  if (subscriptionQuery.isError) return <Alert>Failed to load billing information.</Alert>;
  if (!subscriptionQuery.data) return null;

  const isPro = subscriptionQuery.data.plan === "pro";

  return (
    <div className="mx-auto flex w-full max-w-md flex-col gap-6 rounded border p-6">
      <div>
        <p className="text-xs uppercase tracking-wide text-gray-500">Mock payment provider</p>
        <h1 className="text-2xl font-semibold">{isPro ? "Manage subscription" : "Upgrade to Pro"}</h1>
      </div>

      {isPro ? (
        <p className="text-gray-600">Your organization is currently on the Pro plan.</p>
      ) : (
        <p className="text-gray-600">{MOCK_PRO_PRICE}</p>
      )}

      {subscribeMutation.isError && <Alert>{subscribeMutation.error.message}</Alert>}
      {cancelMutation.isError && <Alert>{cancelMutation.error.message}</Alert>}

      <div className="flex gap-2">
        {isPro ? (
          <Button
            variant="danger"
            onClick={() => cancelMutation.mutate()}
            disabled={cancelMutation.isPending}
          >
            Cancel subscription
          </Button>
        ) : (
          <Button
            onClick={() => subscribeMutation.mutate()}
            disabled={subscribeMutation.isPending}
          >
            Confirm subscription
          </Button>
        )}
        <Link href={`/orgs/${orgId}/billing`} className="self-center text-sm underline">
          Cancel
        </Link>
      </div>
    </div>
  );
}
