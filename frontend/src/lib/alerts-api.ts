import { authedFetch, toApiError } from "./api";

// Mirrors backend/pulse/alerts/rules.py.
export type Comparator = "gt" | "lt" | "gte" | "lte";

export type ThresholdRule = {
  kind: "threshold";
  version: 1;
  comparator: Comparator;
  value: number;
};

export type AnomalyMethod = "zscore" | "seasonal_zscore";

export type AnomalyRule = {
  kind: "anomaly";
  version: 1;
  method: AnomalyMethod;
  window: number;
  sensitivity: number;
};

export type AlertRule = ThresholdRule | AnomalyRule;

export type AlertChannels = {
  email: string[];
  webhook_url: string | null;
  in_app: boolean;
};

export type Alert = {
  id: string;
  name: string;
  insight_id: string;
  rule: AlertRule;
  channels: AlertChannels;
  enabled: boolean;
  is_breaching: boolean;
  last_evaluated_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type AlertEvent = {
  id: string;
  alert_id: string;
  triggered_at: string;
  value: number;
  message: string;
  delivered: Record<string, string>;
  acknowledged_at: string | null;
};

export type EvaluateNowResult = { fired: boolean; recovered: boolean; value: number | null };

function alertsPath(orgId: string, projectId: string): string {
  return `/api/v1/orgs/${orgId}/projects/${projectId}/alerts`;
}

export async function listAlerts(
  accessToken: string,
  orgId: string,
  projectId: string
): Promise<Alert[]> {
  const response = await authedFetch(alertsPath(orgId, projectId), accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function getAlert(
  accessToken: string,
  orgId: string,
  projectId: string,
  alertId: string
): Promise<Alert> {
  const response = await authedFetch(`${alertsPath(orgId, projectId)}/${alertId}`, accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function createAlert(
  accessToken: string,
  orgId: string,
  projectId: string,
  body: { name: string; insight_id: string; rule: AlertRule; channels: AlertChannels }
): Promise<Alert> {
  const response = await authedFetch(alertsPath(orgId, projectId), accessToken, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function updateAlert(
  accessToken: string,
  orgId: string,
  projectId: string,
  alertId: string,
  changes: { name?: string; rule?: AlertRule; channels?: AlertChannels; enabled?: boolean }
): Promise<Alert> {
  const response = await authedFetch(`${alertsPath(orgId, projectId)}/${alertId}`, accessToken, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function deleteAlert(
  accessToken: string,
  orgId: string,
  projectId: string,
  alertId: string
): Promise<void> {
  const response = await authedFetch(`${alertsPath(orgId, projectId)}/${alertId}`, accessToken, {
    method: "DELETE",
  });
  if (!response.ok) throw await toApiError(response);
}

export async function evaluateAlertNow(
  accessToken: string,
  orgId: string,
  projectId: string,
  alertId: string
): Promise<EvaluateNowResult> {
  const response = await authedFetch(
    `${alertsPath(orgId, projectId)}/${alertId}/evaluate-now`,
    accessToken,
    { method: "POST" }
  );
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function listAlertEvents(
  accessToken: string,
  orgId: string,
  projectId: string,
  options: { alertId?: string; unacknowledgedOnly?: boolean } = {}
): Promise<AlertEvent[]> {
  const params = new URLSearchParams();
  if (options.alertId) params.set("alert_id", options.alertId);
  if (options.unacknowledgedOnly) params.set("unacknowledged_only", "true");
  const query = params.toString() ? `?${params.toString()}` : "";
  const response = await authedFetch(`${alertsPath(orgId, projectId)}/events${query}`, accessToken);
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export async function acknowledgeAlertEvent(
  accessToken: string,
  orgId: string,
  projectId: string,
  eventId: string
): Promise<AlertEvent> {
  const response = await authedFetch(
    `${alertsPath(orgId, projectId)}/events/${eventId}/ack`,
    accessToken,
    { method: "POST" }
  );
  if (!response.ok) throw await toApiError(response);
  return response.json();
}
