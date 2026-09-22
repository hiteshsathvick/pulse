import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { QueryResult } from "@/lib/insights-api";
import { InsightResult } from "./InsightResult";
import { RetentionResult } from "./RetentionResult";

describe("InsightResult renders each result type", () => {
  it("trend: shows a chart, and the same numbers in a table view", async () => {
    const result: QueryResult = {
      kind: "trend",
      cached: false,
      approximate: false,
      results: [
        { bucket: "2026-01-01T00:00:00+00:00", value: 3 },
        { bucket: "2026-01-02T00:00:00+00:00", value: 7 },
      ],
    };
    render(<InsightResult result={result} />);

    expect(screen.getByRole("img", { name: /line chart/i })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Bar" }));
    expect(screen.getByRole("img", { name: /bar chart/i })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Table" }));
    const table = screen.getByRole("table");
    expect(within(table).getByText("2026-01-01")).toBeInTheDocument();
    expect(within(table).getByText("7")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Value" })).toBeInTheDocument();
  });

  it("trend with a breakdown: one column per breakdown value", async () => {
    render(
      <InsightResult
        result={{
          kind: "trend",
          cached: false,
          approximate: false,
          results: [
            { bucket: "2026-01-01T00:00:00+00:00", breakdown: "ios", value: 3 },
            { bucket: "2026-01-01T00:00:00+00:00", breakdown: "web", value: 9 },
          ],
        }}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "Table" }));
    expect(screen.getByRole("columnheader", { name: "ios" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "web" })).toBeInTheDocument();
  });

  it("says when unique-user counts are estimates, and stays quiet when they are exact", () => {
    const rows = [{ bucket: "2026-01-01T00:00:00+00:00", value: 100 }];
    const { rerender } = render(
      <InsightResult result={{ kind: "trend", cached: false, approximate: true, results: rows }} />
    );
    expect(screen.getByRole("note")).toHaveTextContent(/estimated/i);

    rerender(
      <InsightResult result={{ kind: "trend", cached: false, approximate: false, results: rows }} />
    );
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("trend with no rows: an empty state, not a blank chart", () => {
    render(<InsightResult result={{ kind: "trend", cached: false, approximate: false, results: [] }} />);
    expect(screen.getByText("No events matched this query.")).toBeInTheDocument();
  });

  it("funnel: each step with its user count, conversion and drop-off", () => {
    render(
      <InsightResult
        result={{
          kind: "funnel",
          cached: false,
          results: [
            { step: "signup", users: 100, conversion_pct: 100, drop_off: 0 },
            { step: "activate", users: 40, conversion_pct: 40, drop_off: 60 },
          ],
        }}
      />
    );
    expect(screen.getByText("1. signup")).toBeInTheDocument();
    expect(screen.getByText("100 users · 100.0%")).toBeInTheDocument();
    expect(screen.getByText("2. activate")).toBeInTheDocument();
    expect(screen.getByText("40 users · 40.0% · 60 dropped")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "activate: 40.0% of the first step" })).toBeInTheDocument();
  });

  it("funnel with a breakdown: a labelled section per value", () => {
    render(
      <InsightResult
        result={{
          kind: "funnel",
          cached: false,
          results: [
            { step: "a", users: 10, conversion_pct: 100, drop_off: 0, breakdown: "ios" },
            { step: "b", users: 5, conversion_pct: 50, drop_off: 5, breakdown: "ios" },
            { step: "a", users: 8, conversion_pct: 100, drop_off: 0, breakdown: "web" },
            { step: "b", users: 2, conversion_pct: 25, drop_off: 6, breakdown: "web" },
          ],
        }}
      />
    );
    expect(screen.getByRole("heading", { name: "ios" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "web" })).toBeInTheDocument();
  });

  it("retention: a cohort grid with percentages, and a dash where a cell isn't observed yet", () => {
    render(
      <InsightResult
        result={{
          kind: "retention",
          period: "week",
          cached: false,
          results: [
            { cohort_period: "2026-01-01T00:00:00+00:00", cohort_size: 10, period_offset: 0, retained: 10, retention_pct: 100 },
            { cohort_period: "2026-01-01T00:00:00+00:00", cohort_size: 10, period_offset: 1, retained: 5, retention_pct: 50 },
            { cohort_period: "2026-01-08T00:00:00+00:00", cohort_size: 4, period_offset: 0, retained: 4, retention_pct: 100 },
          ],
        }}
      />
    );
    const table = screen.getByRole("table");
    expect(within(table).getByText("2026-01-01")).toBeInTheDocument();
    expect(within(table).getByText("2026-01-08")).toBeInTheDocument();
    expect(within(table).getByText("50.0%")).toBeInTheDocument();
    expect(within(table).getByTitle("5 of 10 users")).toBeInTheDocument();
    expect(within(table).getByText("—")).toBeInTheDocument();
  });

  it("retention with no rows: an empty state", () => {
    render(
      <InsightResult result={{ kind: "retention", period: "week", cached: false, results: [] }} />
    );
    expect(screen.getByText("No users were born in this range.")).toBeInTheDocument();
  });

  it("retention: periods that haven't happened yet are a dash, not a misleading 0%", () => {
    const row = (cohort_period: string, period_offset: number, retained: number) => ({
      cohort_period,
      cohort_size: 10,
      period_offset,
      retained,
      retention_pct: retained * 10,
    });
    render(
      <RetentionResult
        period="week"
        now={new Date("2026-01-12T00:00:00Z")}
        rows={[
          // Cohort of 2026-01-05: offset 1 starts 01-12 (started, genuinely 0),
          // offset 2 starts 01-19 (still in the future).
          row("2026-01-05T00:00:00+00:00", 0, 10),
          row("2026-01-05T00:00:00+00:00", 1, 0),
          row("2026-01-05T00:00:00+00:00", 2, 0),
        ]}
      />
    );
    const cells = within(screen.getByRole("table")).getAllByRole("cell");
    // cohort label, users, then offsets 0..2
    expect(cells.slice(2).map((c) => c.textContent)).toEqual(["100.0%", "0.0%", "—"]);
    expect(cells[4]).toHaveAttribute("title", "Not observable yet");
  });

  it("trend: sums of decimals are shown without float noise", async () => {
    render(
      <InsightResult
        result={{
          kind: "trend",
          cached: false,
          approximate: false,
          results: [
            { bucket: "2026-01-01T00:00:00+00:00", value: 88.97999999999999 },
            { bucket: "2026-01-02T00:00:00+00:00", value: 1234 },
          ],
        }}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "Table" }));
    const table = screen.getByRole("table");
    expect(within(table).getByText("88.98")).toBeInTheDocument();
    expect(within(table).queryByText(/88\.9799/)).not.toBeInTheDocument();
    expect(within(table).getByText("1,234")).toBeInTheDocument();
  });
});
