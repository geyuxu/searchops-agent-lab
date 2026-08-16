import { expect, test } from "@playwright/test"

test("operations console loads measured search and ESCI evaluation data", async ({ page }) => {
  await page.goto(process.env.OPERATIONS_CONSOLE_URL || "http://localhost:3001")

  await expect(page.getByRole("heading", { name: "Operations pulse" })).toBeVisible()
  await expect(page.getByText("Search requests · 24h")).toBeVisible()
  await expect(page.getByRole("heading", { name: "Current strategy" })).toBeVisible()
  await expect(page.getByRole("heading", { name: "Popular queries" })).toBeVisible()
  await expect(page.getByRole("heading", { name: "Real offline evaluation runs" })).toBeVisible()
  await expect(page.getByText("No evaluation has been run yet", { exact: false })).toHaveCount(0)
  await expect(page.getByText("Product text + relevance: public Amazon ESCI", { exact: false }).first()).toBeVisible()
})
