"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Alert as AlertBox, Button, EmptyState, Input, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { createAlert, listAlerts, type AlertRule, type Comparator } from "@/lib/alerts-api";
import { listInsights } from "@/lib/insights-api";

const SELECT_CLASS = "rounded border px-3 py-2";

const COMPARATOR_LABEL: Record<Comparator, string> = {
  gt: "is above",
  lt: "is below",
  gte: "is at or above",
  lte: "is at or below",
};

export function AlertList({ orgId, projectId }: { orgId: string; projectId: string }) {
  const { accessToken } = useAuth();
  const queryClient = useQueryClient();

  const [name, setName] = useState("");
  const [insightId, setInsightId] = useState("");
  const [ruleKind, setRuleKind] = useState<"threshold" | "anomaly">("threshold");
  const [comparator, setComparator] = useState<Comparator>("lt");
  const [thresholdValue, setThresholdValue] = useState("");
  const [anomalyWindow, setAnomalyWindow] = useState("14");
  const [anomalySensitivity, setAnomalySensitivity] = useState("3");
  const [email, setEmail] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [inApp, setInApp] = useState(true);
  const [formError, setFormError] = useState<string | null>(null);

  const listKey = ["alerts", orgId, projectId];
  const alertsQuery = useQuery({
    queryKey: listKey,
    queryFn: () => listAlerts(accessToken!, orgId, projectId),
    enabled: !!accessToken,
  });

  const insightsQuery = useQuery({
    queryKey: ["insights", orgId, projectId],
    queryFn: () => listInsights(accessToken!, orgId, projectId),
    enabled: !!accessToken,
  });

  const insights = insightsQuery.data ?? [];
  // An anomaly rule only ever validates against a trend insight (the API
  // would refuse anything else with a 422) -- filtering the picker to match
  // means the form can't even offer an invalid combination.
  const availableInsights =
    ruleKind === "anomaly" ? insights.filter((i) => i.kind === "trend") : insights;

  const createMutation = useMutation({
    mutationFn: () => {
      const rule: AlertRule =
        ruleKind === "threshold"
          ? { kind: "threshold", version: 1, comparator, value: Number(thresholdValue) }
          : {
              kind: "anomaly",
              version: 1,
              method: "zscore",
              window: Number(anomalyWindow),
              sensitivity: Number(anomalySensitivity),
            };
      return createAlert(accessToken!, orgId, projectId, {
        name: name.trim(),
        insight_id: insightId,
        rule,
        channels: {
          email: email.trim() ? [email.trim()] : [],
          webhook_url: webhookUrl.trim() || null,
          in_app: inApp,
        },
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: listKey });
      setName("");
      setThresholdValue("");
      setFormError(null);
    },
    onError: (error: Error) => setFormError(error.message),
  });

  function handleCreate(event: React.FormEvent) {
    event.preventDefault();
    if (
      !name.trim() ||
      !insightId ||
      (ruleKind === "threshold" && thresholdValue.trim() === "")
    ) {
      setFormError("Name, insight, and rule value are all required.");
      return;
    }
    setFormError(null);
    createMutation.mutate();
  }

  if (alertsQuery.isLoading || insightsQuery.isLoading) return <Spinner />;
  if (alertsQuery.isError) return <AlertBox>Failed to load alerts.</AlertBox>;

  return (
    <div className="flex flex-col gap-6">
      <form onSubmit={handleCreate} className="flex flex-col gap-3 rounded border p-4">
        <h2 className="font-medium">New alert</h2>

        <label className="flex flex-col gap-1 text-sm">
          <span>Name</span>
          <Input value={name} onChange={(event) => setName(event.target.value)} />
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span>Insight</span>
          <select
            className={SELECT_CLASS}
            value={insightId}
            onChange={(event) => setInsightId(event.target.value)}
          >
            <option value="">Select an insight…</option>
            {availableInsights.map((insight) => (
              <option key={insight.id} value={insight.id}>
                {insight.name} ({insight.kind})
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span>Rule type</span>
          <select
            className={SELECT_CLASS}
            value={ruleKind}
            onChange={(event) => setRuleKind(event.target.value as "threshold" | "anomaly")}
          >
            <option value="threshold">Threshold</option>
            <option value="anomaly">Anomaly (trend insights only)</option>
          </select>
        </label>

        {ruleKind === "threshold" ? (
          <div className="flex gap-2">
            <select
              className={SELECT_CLASS}
              value={comparator}
              onChange={(event) => setComparator(event.target.value as Comparator)}
              aria-label="Comparator"
            >
              {Object.entries(COMPARATOR_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <Input
              type="number"
              value={thresholdValue}
              onChange={(event) => setThresholdValue(event.target.value)}
              placeholder="Value"
              aria-label="Threshold value"
            />
          </div>
        ) : (
          <div className="flex gap-2">
            <label className="flex flex-col gap-1 text-sm">
              <span>Window (days)</span>
              <Input
                type="number"
                value={anomalyWindow}
                onChange={(event) => setAnomalyWindow(event.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span>Sensitivity (σ)</span>
              <Input
                type="number"
                value={anomalySensitivity}
                onChange={(event) => setAnomalySensitivity(event.target.value)}
              />
            </label>
          </div>
        )}

        <label className="flex flex-col gap-1 text-sm">
          <span>Email (optional)</span>
          <Input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
          />
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span>Webhook URL (optional)</span>
          <Input
            value={webhookUrl}
            onChange={(event) => setWebhookUrl(event.target.value)}
            placeholder="https://example.com/hooks/pulse"
          />
        </label>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={inApp}
            onChange={(event) => setInApp(event.target.checked)}
          />
          <span>Notify in-app</span>
        </label>

        {formError && <AlertBox>{formError}</AlertBox>}
        {createMutation.isError && <AlertBox>{createMutation.error.message}</AlertBox>}

        <div>
          <Button type="submit" disabled={createMutation.isPending}>
            Create alert
          </Button>
        </div>
      </form>

      {alertsQuery.data && alertsQuery.data.length === 0 && (
        <EmptyState>No alerts yet.</EmptyState>
      )}
      <ul className="flex flex-col gap-2">
        {alertsQuery.data?.map((alert) => {
          const insight = insights.find((i) => i.id === alert.insight_id);
          return (
            <li key={alert.id} className="rounded border p-3">
              <Link
                href={`/orgs/${orgId}/projects/${projectId}/alerts/${alert.id}`}
                className="font-medium underline"
              >
                {alert.name}
              </Link>
              <p className="text-sm text-gray-500">
                {insight?.name ?? "unknown insight"} · {alert.rule.kind}
                {alert.is_breaching ? " · breaching" : ""}
                {!alert.enabled ? " · disabled" : ""}
              </p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
