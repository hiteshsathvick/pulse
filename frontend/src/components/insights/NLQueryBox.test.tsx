import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as insightsApi from "@/lib/insights-api";
import { NLQueryBox } from "./NLQueryBox";

vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));

vi.mock("@/lib/insights-api", () => ({
  translateNLQuery: vi.fn(),
  runInsightQuery: vi.fn(),
}));

const translate = vi.mocked(insightsApi.translateNLQuery);
const runQuery = vi.mocked(insightsApi.runInsightQuery);

function renderBox() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <NLQueryBox orgId="org-1" projectId="proj-1" />
    </QueryClientProvider>
  );
}

const TREND_SPEC = {
  kind: "trend" as const,
  version: 1 as const,
  events: ["checkout completed"],
  measure: "count",
  filters: [],
  breakdown: null,
  range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
  granularity: "day" as const,
};

beforeEach(() => {
  vi.clearAllMocks();
});

async function ask(question: string) {
  await userEvent.type(screen.getByPlaceholderText(/how many users completed/i), question);
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
}

describe("NLQueryBox", () => {
  it("shows the interpreted spec before running anything", async () => {
    translate.mockResolvedValue({ status: "ok", spec: TREND_SPEC, message: null, warnings: [] });
    renderBox();

    await ask("how many times did checkout completed happen last week");

    await waitFor(() =>
      expect(translate).toHaveBeenCalledWith(
        "token",
        "org-1",
        "proj-1",
        "how many times did checkout completed happen last week"
      )
    );
    expect(await screen.findByText(/Trend: count of "checkout completed"/)).toBeInTheDocument();
    // Never auto-run: the user must press Run themselves.
    expect(runQuery).not.toHaveBeenCalled();
  });

  it("shows a clarify message instead of a spec when the question is ambiguous", async () => {
    translate.mockResolvedValue({
      status: "clarify",
      spec: null,
      message: "Which event did you mean?",
      warnings: [],
    });
    renderBox();

    await ask("what happened");

    expect(await screen.findByText("Which event did you mean?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
  });

  it("shows grounding warnings alongside an ok spec", async () => {
    translate.mockResolvedValue({
      status: "ok",
      spec: TREND_SPEC,
      message: null,
      warnings: ['"checkout completed" hasn\'t been recorded in this project yet.'],
    });
    renderBox();

    await ask("checkouts");

    expect(await screen.findByText(/hasn't been recorded/)).toBeInTheDocument();
  });

  it("Run sends exactly the interpreted spec and renders the result", async () => {
    translate.mockResolvedValue({ status: "ok", spec: TREND_SPEC, message: null, warnings: [] });
    runQuery.mockResolvedValue({
      kind: "trend",
      cached: false,
      approximate: false,
      results: [{ bucket: "2026-01-01T00:00:00+00:00", value: 42 }],
    });
    renderBox();

    await ask("checkouts last week");
    await screen.findByRole("button", { name: "Run" });
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(runQuery).toHaveBeenCalledWith("token", "org-1", "proj-1", TREND_SPEC));
    await userEvent.click(await screen.findByRole("button", { name: "Table" }));
    expect(within(screen.getByRole("table")).getByText("42")).toBeInTheDocument();
  });

  it("surfaces a translation error without crashing", async () => {
    translate.mockRejectedValue(new Error("Too many natural-language questions right now."));
    renderBox();

    await ask("checkouts");

    expect(await screen.findByText("Too many natural-language questions right now.")).toBeInTheDocument();
  });
});
