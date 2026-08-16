import { Pool, PoolClient } from "pg"
import { calculateSubtotal } from "./domain"

const DATA_NOTICE =
  "Product text: public Amazon ESCI data. Prices, inventory, users and orders: deterministic simulated data."

const globalForPool = globalThis as unknown as { commercePool?: Pool }

export const pool =
  globalForPool.commercePool ||
  new Pool({
    connectionString: process.env.DATABASE_URL,
    max: 8,
    idleTimeoutMillis: 30_000
  })

if (process.env.NODE_ENV !== "production") globalForPool.commercePool = pool

export class CommerceError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message)
  }
}

export async function cart(id: string, client: Pool | PoolClient = pool) {
  const cartResult = await client.query(
    "SELECT id, status, currency, created_at, updated_at FROM lab_carts WHERE id = $1",
    [id]
  )
  if (!cartResult.rowCount) throw new CommerceError(404, "Cart not found")
  const items = await client.query(
    `SELECT id, product_id, title, unit_price, quantity,
            unit_price * quantity AS line_total
       FROM lab_cart_items WHERE cart_id = $1 ORDER BY title, id`,
    [id]
  )
  const subtotal = calculateSubtotal(items.rows)
  return { ...cartResult.rows[0], items: items.rows, subtotal, total: subtotal, data_notice: DATA_NOTICE }
}

export async function order(id: string, client: Pool | PoolClient = pool) {
  const orderResult = await client.query("SELECT * FROM lab_orders WHERE id = $1", [id])
  if (!orderResult.rowCount) throw new CommerceError(404, "Order not found")
  const items = await client.query(
    `SELECT id, product_id, title, unit_price, quantity,
            unit_price * quantity AS line_total
       FROM lab_order_items WHERE order_id = $1 ORDER BY title, id`,
    [id]
  )
  return { ...orderResult.rows[0], items: items.rows }
}

export function requestId(headers: Record<string, unknown>): string {
  const value = headers["x-request-id"]
  return typeof value === "string" && value ? value : crypto.randomUUID()
}

export function respondError(res: { status: (code: number) => { json: (body: unknown) => void } }, error: unknown) {
  const status = error instanceof CommerceError ? error.status : 500
  const message = error instanceof Error ? error.message : "Unknown commerce error"
  res.status(status).json({ error: status === 500 ? "internal_error" : "commerce_error", message })
}

export { DATA_NOTICE }
