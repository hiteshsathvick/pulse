import { expect, test } from "@playwright/test";
import { registerAndLogin, uniqueEmail } from "./helpers";

test("register, log in, and land on the org picker", async ({ page }) => {
  const email = uniqueEmail();
  await registerAndLogin(page, email, "a-strong-password-1");

  await expect(page.getByRole("heading", { name: "Organizations" })).toBeVisible();
  await expect(page.getByText("No organizations yet.")).toBeVisible();

  // The session survives a reload -- the whole point of persisting the
  // refresh token, not just an in-memory access token.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Organizations" })).toBeVisible();
});
