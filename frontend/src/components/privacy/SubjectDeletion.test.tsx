import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as deletionApi from "@/lib/deletion-api";
import * as orgsApi from "@/lib/orgs-api";
import { SubjectDeletion } from "./SubjectDeletion";

vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "token" }) }));
vi.mock("@/lib/orgs-api", () => ({ listMyOrgs: vi.fn() }));
vi.mock("@/lib/deletion-api", () => ({ deleteSubject: vi.fn() }));

const listMyOrgs = vi.mocked(orgsApi.listMyOrgs);
const deleteSubject = vi.mocked(deletionApi.deleteSubject);

const OWNER_ORG: orgsApi.Org = {
  id: "org-1",
  name: "Org",
  slug: "org",
  retention_days: 365,
  role: "owner",
};

function renderForm() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SubjectDeletion orgId="org-1" projectId="proj-1" />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listMyOrgs.mockResolvedValue([OWNER_ORG]);
});

describe("SubjectDeletion", () => {
  it("a non-owner sees an explanation instead of the form", async () => {
    listMyOrgs.mockResolvedValue([{ ...OWNER_ORG, role: "admin" }]);
    renderForm();
    expect(await screen.findByText(/Only an organization Owner/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete this subject/ })).not.toBeInTheDocument();
  });

  it("the delete button is disabled until an identifier is given and DELETE is typed", async () => {
    renderForm();
    const button = await screen.findByRole("button", { name: /Delete this subject/ });
    expect(button).toBeDisabled();

    await userEvent.type(screen.getByLabelText("User ID"), "user-1");
    expect(button).toBeDisabled(); // still needs confirmation typed

    await userEvent.type(screen.getByLabelText(/Type DELETE to confirm/), "DELETE");
    expect(button).toBeEnabled();
  });

  it("typing anything other than the exact phrase leaves it disabled", async () => {
    renderForm();
    await userEvent.type(await screen.findByLabelText("User ID"), "user-1");
    await userEvent.type(screen.getByLabelText(/Type DELETE to confirm/), "delete");
    expect(screen.getByRole("button", { name: /Delete this subject/ })).toBeDisabled();
  });

  it("an anonymous ID alone is also a valid identifier", async () => {
    renderForm();
    await userEvent.type(await screen.findByLabelText("Anonymous ID"), "anon-1");
    await userEvent.type(screen.getByLabelText(/Type DELETE to confirm/), "DELETE");
    expect(screen.getByRole("button", { name: /Delete this subject/ })).toBeEnabled();
  });

  it("submitting calls deleteSubject with the trimmed identifiers and shows the report", async () => {
    deleteSubject.mockResolvedValue({
      rollup_buckets_recomputed: 3,
      rollup_verified: true,
      archive: { entries_removed: 5, objects_rewritten: 2, objects_deleted: 1, unreadable_objects: 0 },
    });
    renderForm();

    await userEvent.type(await screen.findByLabelText("User ID"), "  user-1  ");
    await userEvent.type(screen.getByLabelText(/Type DELETE to confirm/), "DELETE");
    await userEvent.click(screen.getByRole("button", { name: /Delete this subject/ }));

    await waitFor(() =>
      expect(deleteSubject).toHaveBeenCalledWith("token", "org-1", "proj-1", {
        user_id: "user-1",
        anonymous_id: undefined,
      })
    );
    expect(await screen.findByText("Deletion complete.")).toBeInTheDocument();
    expect(screen.getByText(/3 buckets recomputed/)).toBeInTheDocument();
    expect(screen.getByText(/5 entries removed across 3 objects/)).toBeInTheDocument();
  });

  it("clears the form after a successful deletion, requiring re-confirmation for the next one", async () => {
    deleteSubject.mockResolvedValue({
      rollup_buckets_recomputed: 0,
      rollup_verified: true,
      archive: { entries_removed: 0, objects_rewritten: 0, objects_deleted: 0, unreadable_objects: 0 },
    });
    renderForm();

    await userEvent.type(await screen.findByLabelText("User ID"), "user-1");
    await userEvent.type(screen.getByLabelText(/Type DELETE to confirm/), "DELETE");
    await userEvent.click(screen.getByRole("button", { name: /Delete this subject/ }));

    await screen.findByText("Deletion complete.");
    expect(screen.getByLabelText("User ID")).toHaveValue("");
    expect(screen.getByRole("button", { name: /Delete this subject/ })).toBeDisabled();
  });

  it("an unreadable archive object is called out, not silently dropped from the report", async () => {
    deleteSubject.mockResolvedValue({
      rollup_buckets_recomputed: 0,
      rollup_verified: true,
      archive: {
        entries_removed: 1,
        objects_rewritten: 1,
        objects_deleted: 0,
        unreadable_objects: 2,
      },
    });
    renderForm();

    await userEvent.type(await screen.findByLabelText("User ID"), "user-1");
    await userEvent.type(screen.getByLabelText(/Type DELETE to confirm/), "DELETE");
    await userEvent.click(screen.getByRole("button", { name: /Delete this subject/ }));

    expect(
      await screen.findByText(/2 archive objects could not be read and were left untouched/)
    ).toBeInTheDocument();
  });

  it("shows the mutation's error message on failure", async () => {
    deleteSubject.mockRejectedValue(new Error("Project not found"));
    renderForm();

    await userEvent.type(await screen.findByLabelText("User ID"), "user-1");
    await userEvent.type(screen.getByLabelText(/Type DELETE to confirm/), "DELETE");
    await userEvent.click(screen.getByRole("button", { name: /Delete this subject/ }));

    expect(await screen.findByText("Project not found")).toBeInTheDocument();
  });
});
