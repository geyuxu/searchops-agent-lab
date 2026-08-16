import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { cart, CommerceError, pool, requestId, respondError } from "../../../../../../lib/db"
import { validateQuantity } from "../../../../../../lib/domain"

type AddItem = { product_id?: string; quantity?: number }

export async function POST(req: MedusaRequest<AddItem>, res: MedusaResponse) {
  const client = await pool.connect()
  try {
    const productId = req.body?.product_id
    let quantity: number
    try { quantity = validateQuantity(req.body?.quantity ?? 1) }
    catch (error) { throw new CommerceError(400, (error as Error).message) }
    if (!productId) {
      throw new CommerceError(400, "product_id and quantity (1-99) are required")
    }
    await client.query("BEGIN")
    const cartResult = await client.query("SELECT status FROM lab_carts WHERE id = $1 FOR UPDATE", [
      req.params.id
    ])
    if (!cartResult.rowCount) throw new CommerceError(404, "Cart not found")
    if (cartResult.rows[0].status !== "active") throw new CommerceError(409, "Cart is completed")
    const product = await client.query(
      "SELECT product_id, title, price_cents, inventory FROM lab_products WHERE product_id = $1",
      [productId]
    )
    if (!product.rowCount) throw new CommerceError(404, "Product not found in commerce catalog")
    const existing = await client.query(
      "SELECT id, quantity FROM lab_cart_items WHERE cart_id = $1 AND product_id = $2",
      [req.params.id, productId]
    )
    const nextQuantity = quantity + (existing.rowCount ? Number(existing.rows[0].quantity) : 0)
    if (nextQuantity > Number(product.rows[0].inventory)) {
      throw new CommerceError(409, "Requested quantity exceeds simulated inventory")
    }
    await client.query(
      `INSERT INTO lab_cart_items (id, cart_id, product_id, title, unit_price, quantity)
       VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT (cart_id, product_id) DO UPDATE SET quantity = EXCLUDED.quantity`,
      [
        existing.rowCount ? existing.rows[0].id : crypto.randomUUID(),
        req.params.id,
        productId,
        product.rows[0].title,
        product.rows[0].price_cents,
        nextQuantity
      ]
    )
    await client.query("UPDATE lab_carts SET updated_at = now() WHERE id = $1", [req.params.id])
    await client.query("COMMIT")
    res.setHeader("X-Request-ID", requestId(req.headers))
    res.status(201).json({ cart: await cart(req.params.id) })
  } catch (error) {
    await client.query("ROLLBACK")
    respondError(res, error)
  } finally {
    client.release()
  }
}
