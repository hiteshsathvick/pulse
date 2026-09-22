import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as alertsApi from "@/lib/alerts-api";
import type { Insight } from "@/lib/insights-api";
import * as insightsApi from "@/lib/insights-api";
import { AlertList } from "./AlertList";

vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));
vi.mock("@/lib/alerts-api", () => ({ listAlerts: vi.fn(), createAlert: vi.fn() }));
vi.mock("@/lib/insights-api", () => ({ listInsights: vi.fn() }));

const listAlerts = vi.mocked(alertsApi.listAlerts);
const createAlert = vi.mocked(alertsApi.createAlert);
const listInsights = vi.mocked(insightsApi.listInsights);

function insight(id: string, name: string, kind: Insight["kind"]): Insight {
  return {
    id,
    name,
    kind,
    spec: { kind, version: 1 } as unknown as Insight["spec"],
    created_by: "u1",
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
  };
}

function alert(id: string, over: Partial<alertsApi.Alert> = {}): alertsApi.Alert {
  return {
    id,
    name: "Checkout drop",
    insight_id: "insight-trend",
    rule: { kind: "threshold", version: 1, comparator: "lt", value: 5 },
    channels: { email: [], webhook_url: null, in_app: true },
    enabled: true,
    is_breaching: false,
    last_evaluated_at: null,
    created_by: "u1",
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
    ...over,
  };
}

function renderList() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AlertList orgId="org-1" projectId="proj-1" />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listInsights.mockResolvedValue([
    insight("insight-trend", "Checkout trend", "trend"),
    insight("insight-funnel", "Signup funnel", "funnel"),
  ]);
  listAlerts.mockResolvedValue([]);
});

describe("AlertList", () => {
  it("shows an empty state with no alerts", async () => {
    renderList();
    expect(await screen.findByText("No alerts yet.")).toBeInTheDocument();
  });

  it("lists alerts with their insight, rule kind, breaching and disabled status", async () => {
    listAlerts.mockResolvedValue([
      alert("a1", { is_breaching: true }),
      alert("a2", { name: "Retention dip", enabled: false, insight_id: "insight-funnel" }),
    ]);
    renderList();

    const a1 = await screen.findByRole("link", { name: "Checkout drop" });
    expect(a1).toHaveAttribute("href", "/orgs/org-1/projects/proj-1/alerts/a1");
    expect(a1.closest("li")).toHaveTextContent("Checkout trend · threshold · breaching");

    const a2 = screen.getByRole("link", { name: "Retention dip" });
    expect(a2.closest("li")).toHaveTextContent("Signup funnel · threshold · disabled");
  });

  it("the anomaly rule type only offers trend insights", async () => {
    renderList();
    await screen.findByText("New alert");

    await userEvent.selectOptions(screen.getByDisplayValue("Threshold"), "anomaly");

    const picker = screen.getByLabelText("Insight") as HTMLSelectElement;
    const optionLabels = within(picker)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(optionLabels).toEqual(["Select an insight…", "Checkout trend (trend)"]);
  });

  it("creating a threshold alert sends exactly the rule and channels the form describes", async () => {
    createAlert.mockResolvedValue(alert("new-alert"));
    renderList();
    await screen.findByText("New alert");

    await userEvent.type(screen.getByLabelText("Name"), "Too few signups");
    await userEvent.selectOptions(screen.getByLabelText("Insight"), "insight-trend");
    await userEvent.selectOptions(screen.getByLabelText("Comparator"), "lt");
    await userEvent.type(screen.getByLabelText("Threshold value"), "10");
    await userEvent.type(screen.getByLabelText("Email (optional)"), "me@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Create alert" }));

    await waitFor(() => expect(createAlert).toHaveBeenCalledTimes(1));
    expect(createAlert).toHaveBeenCalledWith("token", "org-1", "proj-1", {
      name: "Too few signups",
      insight_id: "insight-trend",
      rule: { kind: "threshold", version: 1, comparator: "lt", value: 10 },
      channels: { email: ["me@example.com"], webhook_url: null, in_app: true },
    });
  });

  it("creating an anomaly alert sends the window and sensitivity as numbers", async () => {
    createAlert.mockResolvedValue(alert("new-alert"));
    renderList();
    await screen.findByText("New alert");

    await userEvent.type(screen.getByLabelText("Name"), "Checkout spike");
    await userEvent.selectOptions(screen.getByDisplayValue("Threshold"), "anomaly");
    await userEvent.selectOptions(screen.getByLabelText("Insight"), "insight-trend");
    await userEvent.click(screen.getByRole("button", { name: "Create alert" }));

    await waitFor(() => expect(createAlert).toHaveBeenCalledTimes(1));
    expect(createAlert.mock.calls[0][3].rule).toEqual({
      kind: "anomaly",
      version: 1,
      method: "zscore",
      window: 14,
      sensitivity: 3,
    });
  });

  it("refuses to submit without a name, insight, or threshold value", async () => {
    renderList();
    await screen.findByText("New alert");

    await userEvent.click(screen.getByRole("button", { name: "Create alert" }));

    expect(await screen.findByText(/all required/)).toBeInTheDocument();
    expect(createAlert).not.toHaveBeenCalled();
  });
});
