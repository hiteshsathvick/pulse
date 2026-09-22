import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as alertsApi from "@/lib/alerts-api";
import { AlertDetail } from "./AlertDetail";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: vi.fn() }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));
vi.mock("@/lib/alerts-api", () => ({
  getAlert: vi.fn(),
  listAlertEvents: vi.fn(),
  updateAlert: vi.fn(),
  deleteAlert: vi.fn(),
  evaluateAlertNow: vi.fn(),
  acknowledgeAlertEvent: vi.fn(),
}));

const getAlert = vi.mocked(alertsApi.getAlert);
const listAlertEvents = vi.mocked(alertsApi.listAlertEvents);
const updateAlert = vi.mocked(alertsApi.updateAlert);
const deleteAlert = vi.mocked(alertsApi.deleteAlert);
const evaluateAlertNow = vi.mocked(alertsApi.evaluateAlertNow);
const acknowledgeAlertEvent = vi.mocked(alertsApi.acknowledgeAlertEvent);

const BASE_ALERT: alertsApi.Alert = {
  id: "alert-1",
  name: "Checkout drop",
  insight_id: "insight-1",
  rule: { kind: "threshold", version: 1, comparator: "lt", value: 5 },
  channels: { email: [], webhook_url: null, in_app: true },
  enabled: true,
  is_breaching: false,
  last_evaluated_at: null,
  created_by: "u1",
  created_at: "2026-09-20T00:00:00Z",
  updated_at: "2026-09-20T00:00:00Z",
};

function event(over: Partial<alertsApi.AlertEvent> = {}): alertsApi.AlertEvent {
  return {
    id: "event-1",
    alert_id: "alert-1",
    triggered_at: "2026-09-22T12:00:00Z",
    value: 2,
    message: '"Checkouts" is 2.00 (lt threshold 5.00)',
    delivered: { in_app: "recorded" },
    acknowledged_at: null,
    ...over,
  };
}

function renderDetail() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AlertDetail orgId="org-1" projectId="proj-1" alertId="alert-1" />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getAlert.mockResolvedValue(BASE_ALERT);
  listAlertEvents.mockResolvedValue([]);
});

describe("AlertDetail", () => {
  it("shows the rule description and current breach status", async () => {
    renderDetail();
    expect(await screen.findByText("Checkout drop")).toBeInTheDocument();
    expect(screen.getByText("Fires when the value is lt 5")).toBeInTheDocument();
    expect(screen.getByText("Not currently breaching")).toBeInTheDocument();
  });

  it("describes an anomaly rule differently from a threshold one", async () => {
    getAlert.mockResolvedValue({
      ...BASE_ALERT,
      rule: { kind: "anomaly", version: 1, method: "zscore", window: 14, sensitivity: 3 },
    });
    renderDetail();
    expect(
      await screen.findByText("Fires on a zscore anomaly (window 14 days, sensitivity 3σ)")
    ).toBeInTheDocument();
  });

  it("toggling enabled calls updateAlert with the flipped value", async () => {
    updateAlert.mockResolvedValue({ ...BASE_ALERT, enabled: false });
    renderDetail();
    await userEvent.click(await screen.findByRole("button", { name: "Disable" }));
    await waitFor(() =>
      expect(updateAlert).toHaveBeenCalledWith("token", "org-1", "proj-1", "alert-1", {
        enabled: false,
      })
    );
  });

  it("delete navigates back to the alerts list", async () => {
    deleteAlert.mockResolvedValue(undefined);
    renderDetail();
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/orgs/org-1/projects/proj-1/alerts"));
  });

  it("evaluate now shows whether it fired, recovered, or changed nothing", async () => {
    evaluateAlertNow.mockResolvedValue({ fired: true, recovered: false, value: 2 });
    renderDetail();
    await userEvent.click(await screen.findByRole("button", { name: "Evaluate now" }));
    expect(await screen.findByText(/^Fired\./)).toHaveTextContent("Current value: 2.00.");
  });

  it("lists events and lets an unacknowledged one be acknowledged", async () => {
    listAlertEvents.mockResolvedValue([event()]);
    acknowledgeAlertEvent.mockResolvedValue(event({ acknowledged_at: "2026-09-22T13:00:00Z" }));
    renderDetail();

    expect(await screen.findByText(/is 2.00/)).toBeInTheDocument();
    expect(screen.getByText(/in_app: recorded/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() =>
      expect(acknowledgeAlertEvent).toHaveBeenCalledWith("token", "org-1", "proj-1", "event-1")
    );
  });

  it("an already-acknowledged event has no Acknowledge button", async () => {
    listAlertEvents.mockResolvedValue([event({ acknowledged_at: "2026-09-22T13:00:00Z" })]);
    renderDetail();
    await screen.findByText(/is 2.00/);
    expect(screen.queryByRole("button", { name: "Acknowledge" })).not.toBeInTheDocument();
  });
});
