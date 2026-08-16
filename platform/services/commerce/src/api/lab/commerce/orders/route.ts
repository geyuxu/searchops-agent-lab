import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { cart, CommerceError, DATA_NOTICE, order, pool, requestId, respondError } from "../../../../lib/db"
import { validCheckout } from "../../../../lib/domain"

type PlaceOrder = {
  cart_id?: string
  email?: string
  shipping_address?: { name?: string; line1?: string; city?: string; postcode?: string; country?: string }
}

export async function POST(req: MedusaRequest<PlaceOrder>, res: MedusaResponse) {
  const client = await pool.connect()
  try {
    const { cart_id: cartId, email, shipping_address: address } = req.body || {}
    const idempotencyKey = String(req.headers["idempotency-key"] || "")
    if (!validCheckout({ cart_id: cartId, email, shipping_address: address })) {
      throw new CommerceError(400, "cart_id, a valid email and complete shipping_address are required")
    }
    if (!idempotencyKey || idempotencyKey.length > 200) {
      throw new CommerceError(400, "Idempotency-Key header is required")
    }
    await client.query("BEGIN")
    const replay = await client.query(
      "SELECT order_id FROM lab_order_idempotency WHERE idempotency_key = $1",
      [idempotencyKey]
    )
    if (replay.rowCount) {
      await client.query("COMMIT")
      res.setHeader("X-Request-ID", requestId(req.headers))
      return res.json({ order: await order(replay.rows[0].order_id) })
    }
    const cartState = await client.query("SELECT status FROM lab_carts WHERE id = $1 FOR UPDATE", [cartId])
    if (!cartState.rowCount) throw new CommerceError(404, "Cart not found")
    if (cartState.rows[0].status !== "active") {
      const existing = await client.query("SELECT id FROM lab_orders WHERE cart_id = $1", [cartId])
      if (existing.rowCount) {
        await client.query("COMMIT")
        return res.json({ order: await order(existing.rows[0].id) })
      }
      throw new CommerceError(409, "Cart is already completed")
    }
    const snapshot = await cart(cartId!, client)
    if (!snapshot.items.length) throw new CommerceError(400, "Cannot place an empty cart")
    for (const item of snapshot.items) {
      const inventory = await client.query(
        "UPDATE lab_products SET inventory = inventory - $1 WHERE product_id = $2 AND inventory >= $1 RETURNING product_id",
        [item.quantity, item.product_id]
      )
      if (!inventory.rowCount) throw new CommerceError(409, `Inventory changed for ${item.product_id}`)
    }
    const orderId = crypto.randomUUID()
    await client.query(
      `INSERT INTO lab_orders
       (id, cart_id, email, subtotal, shipping, total, currency, shipping_address, data_notice)
       VALUES ($1, $2, $3, $4, 0, $4, $5, $6::jsonb, $7)`,
      [orderId, cartId, email, snapshot.subtotal, snapshot.currency, JSON.stringify(address), DATA_NOTICE]
    )
    for (const item of snapshot.items) {
      await client.query(
        `INSERT INTO lab_order_items (id, order_id, product_id, title, unit_price, quantity)
         VALUES ($1, $2, $3, $4, $5, $6)`,
        [crypto.randomUUID(), orderId, item.product_id, item.title, item.unit_price, item.quantity]
      )
    }
    await client.query("UPDATE lab_carts SET status = 'completed', updated_at = now() WHERE id = $1", [cartId])
    await client.query(
      "INSERT INTO lab_order_idempotency (idempotency_key, order_id) VALUES ($1, $2)",
      [idempotencyKey, orderId]
    )
    await client.query("COMMIT")
    res.setHeader("X-Request-ID", requestId(req.headers))
    res.status(201).json({ order: await order(orderId) })
  } catch (error) {
    await client.query("ROLLBACK")
    respondError(res, error)
  } finally {
    client.release()
  }
}
