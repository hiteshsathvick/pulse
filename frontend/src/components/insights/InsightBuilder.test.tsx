import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Insight } from "@/lib/insights-api";
import * as insightsApi from "@/lib/insights-api";
import { InsightBuilder } from "./InsightBuilder";

const replace = vi.fn();
const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));

vi.mock("@/lib/schema-api", () => ({
  listEventSchemas: vi.fn().mockResolvedValue([
    { id: "ev-1", event_name: "checkout completed", status: "active", first_seen_at: "", volume_estimate: 9 },
    { id: "ev-2", event_name: "signup", status: "deprecated", first_seen_at: "", volume_estimate: 5 },
    { id: "ev-3", event_name: "secret internal event", status: "hidden", first_seen_at: "", volume_estimate: 1 },
  ]),
  listPropertySchemas: vi.fn().mockImplementation(async (_t: string, _o: string, _p: string, eventId: string) =>
    eventId === "ev-1"
      ? [
          { id: "p1", event_schema_id: "ev-1", key: "revenue", inferred_type: "number", is_pii: false, status: "active" },
          { id: "p2", event_schema_id: "ev-1", key: "platform", inferred_type: "string", is_pii: false, status: "active" },
          { id: "p3", event_schema_id: "ev-1", key: "old_flag", inferred_type: "bool", is_pii: false, status: "hidden" },
        ]
      : []
  ),
}));

vi.mock("@/lib/insights-api", () => ({
  runInsightQuery: vi.fn(),
  createInsight: vi.fn(),
  updateInsight: vi.fn(),
  deleteInsight: vi.fn(),
}));

const runQuery = vi.mocked(insightsApi.runInsightQuery);
const createInsight = vi.mocked(insightsApi.createInsight);
const updateInsight = vi.mocked(insightsApi.updateInsight);
const deleteInsight = vi.mocked(insightsApi.deleteInsight);

const DATE = /^\d{4}-\d{2}-\d{2}$/;

function renderBuilder(initial?: Insight) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <InsightBuilder orgId="org-1" projectId="proj-1" initial={initial} />
    </QueryClientProvider>
  );
}

function optionValues(testId: string): string[] {
  return within(screen.getByTestId(testId))
    .queryAllByRole("option", { hidden: true })
    .map((o) => (o as HTMLOptionElement).value);
}

beforeEach(() => {
  vi.clearAllMocks();
  runQuery.mockResolvedValue({
    kind: "trend",
    cached: false,
    approximate: false,
    results: [{ bucket: "2026-01-01T00:00:00+00:00", value: 42 }],
  });
});

describe("InsightBuilder — schema-driven autocomplete", () => {
  it("suggests registry events, leaving out hidden ones", async () => {
    renderBuilder();
    await waitFor(() =>
      expect(optionValues("suggestions-Event 1")).toEqual(["checkout completed", "signup"])
    );
  });

  it("suggests the picked event's properties (not hidden ones), numeric-only for a sum", async () => {
    renderBuilder();
    await userEvent.type(screen.getByLabelText("Event 1"), "checkout completed");

    await userEvent.click(screen.getByRole("button", { name: "Add filter" }));
    await waitFor(() =>
      expect(optionValues("suggestions-Filter 1 property")).toEqual(["revenue", "platform"])
    );

    await userEvent.selectOptions(screen.getByLabelText("Measure"), "property_sum");
    expect(optionValues("suggestions-Property to aggregate")).toEqual(["revenue"]);
  });
});

describe("InsightBuilder — emits valid specs", () => {
  it("trend: Run sends exactly the spec the form describes, then shows the result", async () => {
    renderBuilder();
    await userEvent.type(screen.getByLabelText("Event 1"), "checkout completed");
    await userEvent.click(screen.getByRole("button", { name: "Add filter" }));
    await userEvent.type(screen.getByLabelText("Filter 1 property"), "platform");
    await userEvent.type(screen.getByLabelText("Filter 1 value"), "ios");
    await userEvent.type(screen.getByLabelText("Break down by"), "country");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(1));
    expect(runQuery).toHaveBeenCalledWith("token", "org-1", "proj-1", {
      kind: "trend",
      version: 1,
      events: ["checkout completed"],
      measure: "count",
      filters: [{ key: "platform", op: "eq", value: "ios" }],
      breakdown: "country",
      range: { from: expect.stringMatching(DATE), to: expect.stringMatching(DATE), tz: "project" },
      granularity: "day",
    });

    await userEvent.click(await screen.findByRole("button", { name: "Table" }));
    expect(within(screen.getByRole("table")).getByText("42")).toBeInTheDocument();
  });

  it("funnel: steps and window are sent as a funnel spec", async () => {
    runQuery.mockResolvedValue({
      kind: "funnel",
      cached: false,
      results: [
        { step: "signup", users: 10, conversion_pct: 100, drop_off: 0 },
        { step: "activate", users: 4, conversion_pct: 40, drop_off: 6 },
      ],
    });
    renderBuilder();
    await userEvent.click(screen.getByRole("button", { name: "Funnel" }));
    await userEvent.type(screen.getByLabelText("Step 1"), "signup");
    await userEvent.type(screen.getByLabelText("Step 2"), "activate");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(1));
    expect(runQuery.mock.calls[0][3]).toMatchObject({
      kind: "funnel",
      steps: [{ event: "signup" }, { event: "activate" }],
      window: { value: 7, unit: "day" },
    });
    expect(await screen.findByText("2. activate")).toBeInTheDocument();
  });

  it("retention: cohort and return events are sent as a retention spec", async () => {
    runQuery.mockResolvedValue({
      kind: "retention",
      period: "week",
      cached: false,
      results: [
        { cohort_period: "2026-01-01T00:00:00+00:00", cohort_size: 10, period_offset: 0, retained: 10, retention_pct: 100 },
      ],
    });
    renderBuilder();
    await userEvent.click(screen.getByRole("button", { name: "Retention" }));
    await userEvent.type(screen.getByLabelText("Cohort event"), "signup");
    await userEvent.type(screen.getByLabelText("Return event"), "signup");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(1));
    expect(runQuery.mock.calls[0][3]).toMatchObject({
      kind: "retention",
      born_event: "signup",
      return_event: "signup",
      period: "week",
      periods: 8,
    });
    expect(await screen.findByText("100.0%")).toBeInTheDocument();
  });

  it("an incomplete form shows why and sends nothing", async () => {
    renderBuilder();
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Choose at least one event.")).toBeInTheDocument();
    expect(runQuery).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Save insight" }));
    expect(screen.getByText("Give the insight a name before saving.")).toBeInTheDocument();
    expect(createInsight).not.toHaveBeenCalled();
  });
});

describe("InsightBuilder — saving", () => {
  it("creates an insight from the built spec, then moves to its own page", async () => {
    createInsight.mockResolvedValue({ id: "new-id" } as Insight);
    renderBuilder();
    await userEvent.type(screen.getByLabelText("Insight name"), "Weekly checkouts");
    await userEvent.type(screen.getByLabelText("Event 1"), "checkout completed");
    await userEvent.click(screen.getByRole("button", { name: "Save insight" }));

    await waitFor(() => expect(createInsight).toHaveBeenCalledTimes(1));
    expect(createInsight).toHaveBeenCalledWith(
      "token",
      "org-1",
      "proj-1",
      "Weekly checkouts",
      expect.objectContaining({ kind: "trend", events: ["checkout completed"] })
    );
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("/orgs/org-1/projects/proj-1/insights/new-id")
    );
  });

  it("surfaces an API failure instead of failing silently", async () => {
    createInsight.mockRejectedValue(new Error("Requires at least the member role"));
    renderBuilder();
    await userEvent.type(screen.getByLabelText("Insight name"), "x");
    await userEvent.type(screen.getByLabelText("Event 1"), "signup");
    await userEvent.click(screen.getByRole("button", { name: "Save insight" }));
    expect(await screen.findByText("Requires at least the member role")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("InsightBuilder — a saved insight reloads", () => {
  const saved: Insight = {
    id: "ins-1",
    name: "Signup to activation",
    kind: "funnel",
    created_by: "u",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    spec: {
      kind: "funnel",
      version: 1,
      steps: [{ event: "signup" }, { event: "activate" }, { event: "purchase" }],
      window: { value: 3, unit: "hour" },
      filters: [{ key: "platform", op: "neq", value: "bot" }],
      breakdown: "country",
      range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
    },
  };

  beforeEach(() => {
    runQuery.mockResolvedValue({
      kind: "funnel",
      cached: false,
      results: [
        { step: "signup", users: 10, conversion_pct: 100, drop_off: 0, breakdown: "IN" },
        { step: "activate", users: 5, conversion_pct: 50, drop_off: 5, breakdown: "IN" },
        { step: "purchase", users: 1, conversion_pct: 10, drop_off: 4, breakdown: "IN" },
      ],
    });
  });

  it("pre-fills the form from the stored spec and runs it without a click", async () => {
    renderBuilder(saved);

    expect(screen.getByLabelText("Insight name")).toHaveValue("Signup to activation");
    expect(screen.getByLabelText("Step 1")).toHaveValue("signup");
    expect(screen.getByLabelText("Step 3")).toHaveValue("purchase");
    expect(screen.getByLabelText("Conversion window")).toHaveValue(3);
    expect(screen.getByLabelText("Window unit")).toHaveValue("hour");
    expect(screen.getByLabelText("Filter 1 property")).toHaveValue("platform");
    expect(screen.getByLabelText("Filter 1 operator")).toHaveValue("neq");
    expect(screen.getByLabelText("Break down by")).toHaveValue("country");

    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(1));
    expect(runQuery).toHaveBeenCalledWith("token", "org-1", "proj-1", saved.spec);
    expect(await screen.findByText("3. purchase")).toBeInTheDocument();
  });

  it("saves changes back to the same insight", async () => {
    updateInsight.mockResolvedValue(saved);
    renderBuilder(saved);
    await userEvent.clear(screen.getByLabelText("Insight name"));
    await userEvent.type(screen.getByLabelText("Insight name"), "Renamed");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateInsight).toHaveBeenCalledTimes(1));
    expect(updateInsight).toHaveBeenCalledWith("token", "org-1", "proj-1", "ins-1", {
      name: "Renamed",
      spec: saved.spec,
    });
    expect(createInsight).not.toHaveBeenCalled();
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("deletes only after confirmation, then returns to the list", async () => {
    deleteInsight.mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm");
    renderBuilder(saved);

    confirm.mockReturnValueOnce(false);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(deleteInsight).not.toHaveBeenCalled();

    confirm.mockReturnValueOnce(true);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(deleteInsight).toHaveBeenCalledWith("token", "org-1", "proj-1", "ins-1")
    );
    await waitFor(() => expect(push).toHaveBeenCalledWith("/orgs/org-1/projects/proj-1/insights"));
    confirm.mockRestore();
  });
});
