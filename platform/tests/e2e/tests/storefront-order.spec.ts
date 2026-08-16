import { expect, test } from "@playwright/test"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

function realEsciQuery(): string {
  const path = resolve(__dirname, "../../../data/processed/queries.jsonl")
  const first = readFileSync(path, "utf8").split("\n").find(Boolean)
  if (!first) throw new Error("No processed ESCI query. Run make data and make seed first.")
  return JSON.parse(first).query
}

test("search → detail → cart → checkout → order", async ({ page }) => {
  await page.goto("/")
  await page.evaluate(() => localStorage.clear())
  await page.reload()

  await page.getByTestId("search-input").fill(realEsciQuery())
  await page.getByTestId("search-submit").click()
  const firstProduct = page.getByTestId("product-card").first()
  await expect(firstProduct).toBeVisible()
  await firstProduct.locator("a").first().click()

  await expect(page.getByTestId("product-detail")).toBeVisible()
  const itemAdded = page.waitForResponse(response =>
    response.request().method() === "POST"
      && /\/api\/commerce\/carts\/[^/]+\/items$/.test(new URL(response.url()).pathname)
      && response.ok()
  )
  await page.getByTestId("detail-add-to-cart").click()
  await itemAdded
  await page.getByRole("link", { name: "Cart / checkout" }).click()

  await expect(page.getByText("Where should it go?")).toBeVisible()
  await expect(page.getByTestId("place-order")).toBeEnabled()
  await page.getByTestId("place-order").click()

  await expect(page.getByTestId("order-result")).toBeVisible()
  await expect(page.getByText("ORDER CREATED")).toBeVisible()
  await expect(page.getByText("Public product relevance, simulated commerce.", { exact: false })).toBeVisible()
})
