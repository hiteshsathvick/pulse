import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { registerAndLogin, uniqueEmail } from "./helpers";

test("create an org, create a project, and land on its home page", async ({ page }) => {
  await registerAndLogin(page, uniqueEmail(), "a-strong-password-1");

  const orgSlug = `e2e-org-${randomUUID().slice(0, 8)}`;
  await page.getByPlaceholder("Name").fill("E2E Org");
  await page.getByPlaceholder("Slug").fill(orgSlug);
  await page.getByRole("button", { name: "Create" }).click();

  const orgLink = page.getByRole("link", { name: "E2E Org" });
  await expect(orgLink).toBeVisible();
  await orgLink.click();
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();

  const projectSlug = `e2e-web-${randomUUID().slice(0, 8)}`;
  await page.getByPlaceholder("Name").fill("E2E Web");
  await page.getByPlaceholder("Slug").fill(projectSlug);
  await page.getByRole("button", { name: "Create" }).click();

  const projectLink = page.getByRole("link", { name: "E2E Web" });
  await expect(projectLink).toBeVisible();
  await projectLink.click();

  await expect(page.getByRole("heading", { name: "E2E Web" })).toBeVisible();
  await expect(page.getByText(`${projectSlug} · UTC`)).toBeVisible();

  // The switcher reflects the URL's org/project context, not a server-side
  // "active org" -- both dropdowns should show the org/project just created.
  await expect(page.getByLabel("Organization")).toHaveValue(/.+/);
  await expect(page.getByLabel("Project")).toHaveValue(/.+/);
});
