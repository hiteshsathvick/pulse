import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Dashboard } from "@/lib/dashboards-api";
import * as dashboardsApi from "@/lib/dashboards-api";
import * as insightsApi from "@/lib/insights-api";
import type { InsightSpec } from "@/lib/insight-spec";
import { DashboardView } from "./DashboardView";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));
vi.mock("@/lib/projects-api", () => ({
  getProject: vi
    .fn()
    .mockResolvedValue({ id: "proj-1", org_id: "org-1", name: "Web", slug: "web", timezone: "Asia/Kolkata" }),
}));
vi.mock("@/lib/insights-api", () => ({ runInsightQuery: vi.fn(), listInsights: vi.fn() }));
vi.mock("@/lib/dashboards-api", () => ({ updateDashboard: vi.fn(), deleteDashboard: vi.fn() }));

const runQuery = vi.mocked(insightsApi.runInsightQuery);
const DAY_MS = 86_400_000;

function funnelSpec(first: string, second: string): InsightSpec {
  return {
    kind: "funnel",
    version: 1,
    steps: [{ event: first }, { event: second }],
    window: { value: 7, unit: "day" },
    filters: [],
    breakdown: null,
    // Deliberately old: the dashboard's range must override this.
    range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
  };
}

function item(id: string, name: string, spec: InsightSpec, x: number) {
  return {
    id: `item-${id}`,
    insight: { id, name, kind: spec.kind, spec },
    position: { x, y: 0, w: 6, h: 3 },
  };
}

function makeDashboard(overrides: Partial<Dashboard> = {}): Dashboard {
  return {
    id: "d1",
    name: "Growth",
    layout: { columns: 12 },
    default_range: { type: "relative", days: 30 },
    shared_scope: "org",
    created_by: "u1",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    can_edit: true,
    items: [
      item("a", "Insight A", funnelSpec("signup", "activate"), 0),
      item("b", "Insight B", funnelSpec("view", "buy"), 6),
    ],
    ...overrides,
  };
}

function renderView(dashboard: Dashboard) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DashboardView orgId="org-1" projectId="proj-1" dashboard={dashboard} />
    </QueryClientProvider>
  );
}

const spanDays = (spec: InsightSpec) =>
  (Date.parse(spec.range.to) - Date.parse(spec.range.from)) / DAY_MS;

beforeEach(() => {
  vi.clearAllMocks();
  runQuery.mockImplementation(async (_t, _o, _p, spec) => {
    if (spec.kind !== "funnel") throw new Error("unexpected kind");
    return {
      kind: "funnel",
      cached: false,
      results: [
        { step: spec.steps[0].event, users: 10, conversion_pct: 100, drop_off: 0 },
        { step: spec.steps[1].event, users: 4, conversion_pct: 40, drop_off: 6 },
      ],
    };
  });
  vi.mocked(insightsApi.listInsights).mockResolvedValue([]);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("DashboardView", () => {
  it("shows every insight on the dashboard, each with its own result", async () => {
    renderView(makeDashboard());
    const a = await screen.findByRole("region", { name: "Insight A" });
    const b = await screen.findByRole("region", { name: "Insight B" });
    expect(await within(a).findByText("1. signup")).toBeInTheDocument();
    expect(await within(b).findByText("1. view")).toBeInTheDocument();
  });

  it("runs each insight over the dashboard's range, not the dates it was saved with", async () => {
    renderView(makeDashboard());
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(2));

    for (const call of runQuery.mock.calls) {
      const spec = call[3];
      expect(spec.range.from).not.toBe("2026-01-01");
      expect(spanDays(spec)).toBe(29); // "last 30 days" counts today
      expect(spec.range.tz).toBe("project"); // everything but from/to is as saved
      expect(call[4]).toEqual({ refresh: false }); // first load may use the cache
    }
    const funnels = runQuery.mock.calls.map((c) => c[3]);
    expect(funnels.map((s) => (s.kind === "funnel" ? s.steps[0].event : ""))).toEqual(
      expect.arrayContaining(["signup", "view"])
    );
  });

  it("Refresh re-runs every insight and asks the server to bypass its cache", async () => {
    renderView(makeDashboard());
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(2));

    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(4));
    expect(runQuery.mock.calls.slice(2).map((c) => c[4])).toEqual([
      { refresh: true },
      { refresh: true },
    ]);
  });

  it("changing the range re-queries with it, and saves nothing", async () => {
    renderView(makeDashboard());
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(2));

    await userEvent.selectOptions(screen.getByLabelText("Date range"), "7");

    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(4));
    for (const call of runQuery.mock.calls.slice(2)) {
      expect(spanDays(call[3])).toBe(6);
      expect(call[4]).toEqual({ refresh: false }); // a new range isn't a refresh
    }
    expect(dashboardsApi.updateDashboard).not.toHaveBeenCalled();
  });

  it("auto-refresh re-runs the insights on its interval, and stops when turned off", async () => {
    // Only the interval is faked; RTL and TanStack Query keep their real timers.
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderView(makeDashboard());
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(2));

    const select = screen.getByLabelText("Auto-refresh");
    await act(async () => {
      (select as HTMLSelectElement).value = "60000";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });

    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(4));
    expect(runQuery.mock.calls[3][4]).toEqual({ refresh: true });

    await act(async () => {
      (select as HTMLSelectElement).value = "0";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      vi.advanceTimersByTime(300_000);
    });
    expect(runQuery).toHaveBeenCalledTimes(4);
  });

  it("one insight failing doesn't take the others down", async () => {
    runQuery.mockImplementation(async (_t, _o, _p, spec) => {
      if (spec.kind === "funnel" && spec.steps[0].event === "view") throw new Error("boom");
      return {
        kind: "funnel",
        cached: false,
        results: [{ step: "signup", users: 1, conversion_pct: 100, drop_off: 0 }],
      };
    });
    renderView(makeDashboard());

    const b = await screen.findByRole("region", { name: "Insight B" });
    expect(await within(b).findByText("boom")).toBeInTheDocument();
    const a = screen.getByRole("region", { name: "Insight A" });
    expect(await within(a).findByText("1. signup")).toBeInTheDocument();
  });

  it("a read-only viewer can look and change their own range, but not edit", async () => {
    renderView(makeDashboard({ can_edit: false }));
    await screen.findByRole("region", { name: "Insight A" });

    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.getByText(/Read-only: you can view this dashboard but not change it/)).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Date range"), "14");
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(4));
    expect(dashboardsApi.updateDashboard).not.toHaveBeenCalled();
  });

  it("someone who can edit gets an Edit button, and it opens the editor", async () => {
    renderView(makeDashboard());
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(await screen.findByRole("heading", { name: "Insights on this dashboard" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save dashboard" })).toBeInTheDocument();
  });

  it("an empty dashboard opens in the editor for its editor, and says so for everyone else", async () => {
    const { unmount } = renderView(makeDashboard({ items: [] }));
    expect(await screen.findByRole("heading", { name: "Insights on this dashboard" })).toBeInTheDocument();
    unmount();

    renderView(makeDashboard({ items: [], can_edit: false }));
    expect(await screen.findByText("This dashboard has no insights yet.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Insights on this dashboard" })).not.toBeInTheDocument();
  });

  it("states who can see it and which dates it is showing", async () => {
    renderView(makeDashboard({ shared_scope: "private" }));
    expect(await screen.findByText(/Only you · Last 30 days \(\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}\)/)).toBeInTheDocument();
  });
});
