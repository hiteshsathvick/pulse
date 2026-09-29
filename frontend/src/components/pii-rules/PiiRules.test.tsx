import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as orgsApi from "@/lib/orgs-api";
import * as piiRulesApi from "@/lib/pii-rules-api";
import { PiiRules } from "./PiiRules";

vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));
vi.mock("@/lib/orgs-api", () => ({ listMyOrgs: vi.fn() }));
vi.mock("@/lib/pii-rules-api", () => ({
  listPiiRules: vi.fn(),
  createPiiRule: vi.fn(),
  deletePiiRule: vi.fn(),
}));

const listMyOrgs = vi.mocked(orgsApi.listMyOrgs);
const listPiiRules = vi.mocked(piiRulesApi.listPiiRules);
const createPiiRule = vi.mocked(piiRulesApi.createPiiRule);
const deletePiiRule = vi.mocked(piiRulesApi.deletePiiRule);

const ADMIN_ORG: orgsApi.Org = {
  id: "org-1",
  name: "Org",
  slug: "org",
  retention_days: 365,
  role: "admin",
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PiiRules orgId="org-1" projectId="proj-1" />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listMyOrgs.mockResolvedValue([ADMIN_ORG]);
  listPiiRules.mockResolvedValue([]);
});

describe("PiiRules", () => {
  it("a member sees an explanation instead of the form", async () => {
    listMyOrgs.mockResolvedValue([{ ...ADMIN_ORG, role: "member" }]);
    renderPage();
    expect(await screen.findByText(/Only an organization Admin or Owner/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create rule" })).not.toBeInTheDocument();
    expect(listPiiRules).not.toHaveBeenCalled();
  });

  it("an admin (not just an owner) can see and use the page", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: "Create rule" })).toBeInTheDocument();
    await waitFor(() => expect(listPiiRules).toHaveBeenCalledWith("token", "org-1", "proj-1"));
  });

  it("shows an empty state when there are no rules yet", async () => {
    renderPage();
    expect(await screen.findByText("No PII rules yet.")).toBeInTheDocument();
  });

  it("lists existing rules with their action", async () => {
    listPiiRules.mockResolvedValue([
      { id: "rule-1", property_key: "email", action: "hash", created_at: "2026-01-01T00:00:00Z" },
      { id: "rule-2", property_key: "ssn", action: "drop", created_at: "2026-01-01T00:00:00Z" },
    ]);
    renderPage();
    // The option labels in the "New rule" select repeat this same text, so
    // scope each assertion to its list row rather than matching page-wide.
    const emailRow = (await screen.findByText("email")).closest("li")!;
    expect(emailRow).toHaveTextContent("Hash (deterministic, non-reversible)");
    const ssnRow = screen.getByText("ssn").closest("li")!;
    expect(ssnRow).toHaveTextContent("Drop (delete the property entirely)");
  });

  it("the create button is disabled until a property key is given", async () => {
    renderPage();
    const button = await screen.findByRole("button", { name: "Create rule" });
    expect(button).toBeDisabled();

    await userEvent.type(screen.getByPlaceholderText("e.g. email"), "email");
    expect(button).toBeEnabled();
  });

  it("submitting calls createPiiRule with the trimmed key and chosen action, and shows the report", async () => {
    createPiiRule.mockResolvedValue({
      id: "rule-1",
      property_key: "email",
      action: "hash",
      created_at: "2026-01-01T00:00:00Z",
      archive_rewrite: { entries_scrubbed: 4, objects_rewritten: 2, unreadable_objects: 0 },
    });
    renderPage();

    await userEvent.type(await screen.findByPlaceholderText("e.g. email"), "  email  ");
    await userEvent.selectOptions(screen.getByLabelText("Action"), "hash");
    await userEvent.click(screen.getByRole("button", { name: "Create rule" }));

    await waitFor(() =>
      expect(createPiiRule).toHaveBeenCalledWith("token", "org-1", "proj-1", {
        property_key: "email",
        action: "hash",
      })
    );
    expect(await screen.findByText("Rule created.")).toBeInTheDocument();
    expect(screen.getByText(/4 entries scrubbed across 2 objects/)).toBeInTheDocument();
  });

  it("an unreadable archive object is called out on creation", async () => {
    createPiiRule.mockResolvedValue({
      id: "rule-1",
      property_key: "email",
      action: "hash",
      created_at: "2026-01-01T00:00:00Z",
      archive_rewrite: { entries_scrubbed: 1, objects_rewritten: 1, unreadable_objects: 2 },
    });
    renderPage();

    await userEvent.type(await screen.findByPlaceholderText("e.g. email"), "email");
    await userEvent.click(screen.getByRole("button", { name: "Create rule" }));

    expect(
      await screen.findByText(/2 archive objects could not be read and were left unscrubbed/)
    ).toBeInTheDocument();
  });

  it("a null archive_rewrite (store unreachable) is reported, not treated as success", async () => {
    createPiiRule.mockResolvedValue({
      id: "rule-1",
      property_key: "email",
      action: "hash",
      created_at: "2026-01-01T00:00:00Z",
      archive_rewrite: null,
    });
    renderPage();

    await userEvent.type(await screen.findByPlaceholderText("e.g. email"), "email");
    await userEvent.click(screen.getByRole("button", { name: "Create rule" }));

    expect(await screen.findByText("Rule created.")).toBeInTheDocument();
    expect(screen.getByText(/the archive could not be reached/)).toBeInTheDocument();
  });

  it("clears the property key field after a successful create", async () => {
    createPiiRule.mockResolvedValue({
      id: "rule-1",
      property_key: "email",
      action: "hash",
      created_at: "2026-01-01T00:00:00Z",
      archive_rewrite: null,
    });
    renderPage();

    await userEvent.type(await screen.findByPlaceholderText("e.g. email"), "email");
    await userEvent.click(screen.getByRole("button", { name: "Create rule" }));

    await screen.findByText("Rule created.");
    expect(screen.getByPlaceholderText("e.g. email")).toHaveValue("");
  });

  it("shows the mutation's error message on a duplicate property key", async () => {
    createPiiRule.mockRejectedValue(
      new Error("A rule for this property already exists on this project")
    );
    renderPage();

    await userEvent.type(await screen.findByPlaceholderText("e.g. email"), "email");
    await userEvent.click(screen.getByRole("button", { name: "Create rule" }));

    expect(
      await screen.findByText("A rule for this property already exists on this project")
    ).toBeInTheDocument();
  });

  it("deleting a rule asks for confirmation first", async () => {
    listPiiRules.mockResolvedValue([
      { id: "rule-1", property_key: "email", action: "hash", created_at: "2026-01-01T00:00:00Z" },
    ]);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));

    expect(window.confirm).toHaveBeenCalledWith(
      'Stop scrubbing "email"? Future events will keep it as-is.'
    );
    expect(deletePiiRule).not.toHaveBeenCalled();
  });

  it("confirming deletion calls deletePiiRule and refreshes the list", async () => {
    listPiiRules.mockResolvedValue([
      { id: "rule-1", property_key: "email", action: "hash", created_at: "2026-01-01T00:00:00Z" },
    ]);
    deletePiiRule.mockResolvedValue(undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(deletePiiRule).toHaveBeenCalledWith("token", "org-1", "proj-1", "rule-1")
    );
  });
});
