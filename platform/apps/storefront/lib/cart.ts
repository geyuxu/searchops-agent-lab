import type { Cart } from "./types"

const CART_KEY = "searchops-lab-cart-id"

async function json<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.message || `Request failed (${response.status})`)
  return body as T
}

export async function ensureCart(): Promise<Cart> {
  const existing = window.localStorage.getItem(CART_KEY)
  if (existing) {
    const response = await fetch(`/api/commerce/carts/${existing}`, { cache: "no-store" })
    if (response.ok) return (await response.json()).cart
    window.localStorage.removeItem(CART_KEY)
  }
  const created = await json<{ cart: Cart }>(
    await fetch("/api/commerce/carts", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" })
  )
  window.localStorage.setItem(CART_KEY, created.cart.id)
  return created.cart
}

export async function addToCart(productId: string, quantity = 1): Promise<Cart> {
  const current = await ensureCart()
  const response = await fetch(`/api/commerce/carts/${current.id}/items`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ product_id: productId, quantity })
  })
  return (await json<{ cart: Cart }>(response)).cart
}

export async function getCart(): Promise<Cart> {
  return ensureCart()
}

export function clearCart() {
  window.localStorage.removeItem(CART_KEY)
}

