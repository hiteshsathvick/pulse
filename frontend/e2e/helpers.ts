import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";

export function uniqueEmail(): string {
  return `e2e-${randomUUID()}@example.com`;
}

export async function registerAndLogin(page: Page, email: string, password: string): Promise<void> {
  await page.goto("/register");
  await page.getByPlaceholder("Name").fill("E2E Test User");
  await page.getByPlaceholder("Email").fill(email);
  await page.getByPlaceholder("Password (min 8 characters)").fill(password);
  await page.getByRole("button", { name: "Register" }).click();

  await page.waitForURL("**/login?registered=1");
  await page.getByPlaceholder("Email").fill(email);
  await page.getByPlaceholder("Password").fill(password);
  await page.getByRole("button", { name: "Log in" }).click();

  await page.waitForURL("**/orgs");
}
