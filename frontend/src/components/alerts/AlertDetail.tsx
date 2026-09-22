"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { Alert as AlertBox, Button, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import {
  acknowledgeAlertEvent,
  deleteAlert,
  evaluateAlertNow,
  getAlert,
  listAlertEvents,
  updateAlert,
} from "@/lib/alerts-api";

export function AlertDetail({
  orgId,
  projectId,
  alertId,
}: {
  orgId: string;
  projectId: string;
  alertId: string;
}) {
  const { accessToken } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();

  const alertKey = ["alert", orgId, projectId, alertId];
  const alertQuery = useQuery({
    queryKey: alertKey,
    queryFn: () => getAlert(accessToken!, orgId, projectId, alertId),
    enabled: !!accessToken,
  });

  const eventsKey = ["alert-events", orgId, projectId, alertId];
  const eventsQuery = useQuery({
    queryKey: eventsKey,
    queryFn: () => listAlertEvents(accessToken!, orgId, projectId, { alertId }),
    enabled: !!accessToken,
  });

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      updateAlert(accessToken!, orgId, projectId, alertId, { enabled }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: alertKey }),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteAlert(accessToken!, orgId, projectId, alertId),
    onSuccess: () => router.push(`/orgs/${orgId}/projects/${projectId}/alerts`),
  });

  // The same evaluate_alert() path the alert-worker runs on its own
  // schedule -- useful for testing a rule or demoing it without waiting.
  const evaluateMutation = useMutation({
    mutationFn: () => evaluateAlertNow(accessToken!, orgId, projectId, alertId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: alertKey });
      void queryClient.invalidateQueries({ queryKey: eventsKey });
    },
  });

  const ackMutation = useMutation({
    mutationFn: (eventId: string) =>
      acknowledgeAlertEvent(accessToken!, orgId, projectId, eventId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: eventsKey }),
  });

  if (alertQuery.isLoading) return <Spinner />;
  if (alertQuery.isError) return <AlertBox>Failed to load this alert.</AlertBox>;
  if (!alertQuery.data) return null;

  const alert = alertQuery.data;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{alert.name}</h1>
          <p className="text-sm text-gray-500">
            {alert.rule.kind === "threshold"
              ? `Fires when the value is ${alert.rule.comparator} ${alert.rule.value}`
              : `Fires on a ${alert.rule.method} anomaly (window ${alert.rule.window} days, sensitivity ${alert.rule.sensitivity}σ)`}
          </p>
          <p className="text-sm text-gray-500">
            {alert.is_breaching ? "Currently breaching" : "Not currently breaching"}
            {alert.last_evaluated_at &&
              ` · last evaluated ${new Date(alert.last_evaluated_at).toLocaleString()}`}
            {!alert.enabled && " · disabled"}
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            onClick={() => toggleMutation.mutate(!alert.enabled)}
            disabled={toggleMutation.isPending}
          >
            {alert.enabled ? "Disable" : "Enable"}
          </Button>
          <Button
            variant="secondary"
            onClick={() => evaluateMutation.mutate()}
            disabled={evaluateMutation.isPending}
          >
            Evaluate now
          </Button>
          <Button
            variant="danger"
            onClick={() => deleteMutation.mutate()}
            disabled={deleteMutation.isPending}
          >
            Delete
          </Button>
        </div>
      </div>

      {evaluateMutation.isError && <AlertBox>{evaluateMutation.error.message}</AlertBox>}
      {evaluateMutation.isSuccess && (
        <AlertBox variant="success">
          {evaluateMutation.data.fired
            ? "Fired."
            : evaluateMutation.data.recovered
              ? "Recovered."
              : "No change."}
          {evaluateMutation.data.value !== null &&
            ` Current value: ${evaluateMutation.data.value.toFixed(2)}.`}
        </AlertBox>
      )}

      <div>
        <h2 className="mb-2 font-medium">Recent events</h2>
        {eventsQuery.isLoading && <Spinner />}
        {eventsQuery.isError && <AlertBox>Failed to load events.</AlertBox>}
        {eventsQuery.data && eventsQuery.data.length === 0 && (
          <p className="text-gray-500">No events yet.</p>
        )}
        <ul className="flex flex-col gap-2">
          {eventsQuery.data?.map((event) => (
            <li key={event.id} className="flex items-center justify-between rounded border p-3">
              <div>
                <p>{event.message}</p>
                <p className="text-sm text-gray-500">
                  {new Date(event.triggered_at).toLocaleString()} ·{" "}
                  {Object.entries(event.delivered)
                    .map(([channel, status]) => `${channel}: ${status}`)
                    .join(", ")}
                </p>
              </div>
              {!event.acknowledged_at && (
                <Button
                  variant="secondary"
                  onClick={() => ackMutation.mutate(event.id)}
                  disabled={ackMutation.isPending}
                >
                  Acknowledge
                </Button>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
