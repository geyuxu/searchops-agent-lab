import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { cart, CommerceError, pool, requestId, respondError } from "../../../../../../../lib/db"
import { validateQuantity } from "../../../../../../../lib/domain"

type UpdateItem = { quantity?: number }

export async function PATCH(req: MedusaRequest<UpdateItem>, res: MedusaResponse) {
  try {
    let quantity: number
    try { quantity = validateQuantity(req.body?.quantity) }
    catch (error) { throw new CommerceError(400, (error as Error).message) }
    const result = await pool.query(
      `UPDATE lab_cart_items i SET quantity = $1
       FROM lab_products p, lab_carts c
       WHERE i.id = $2 AND i.cart_id = $3 AND p.product_id = i.product_id
         AND c.id = i.cart_id AND c.status = 'active' AND $1 <= p.inventory
       RETURNING i.id`,
      [quantity, req.params.itemId, req.params.id]
    )
    if (!result.rowCount) throw new CommerceError(409, "Item update rejected or inventory unavailable")
    await pool.query("UPDATE lab_carts SET updated_at = now() WHERE id = $1", [req.params.id])
    res.setHeader("X-Request-ID", requestId(req.headers))
    res.json({ cart: await cart(req.params.id) })
  } catch (error) {
    respondError(res, error)
  }
}

export async function DELETE(req: MedusaRequest, res: MedusaResponse) {
  try {
    const result = await pool.query(
      `DELETE FROM lab_cart_items i USING lab_carts c
       WHERE i.id = $1 AND i.cart_id = $2 AND c.id = i.cart_id AND c.status = 'active'
       RETURNING i.id`,
      [req.params.itemId, req.params.id]
    )
    if (!result.rowCount) throw new CommerceError(404, "Active cart item not found")
    res.setHeader("X-Request-ID", requestId(req.headers))
    res.json({ cart: await cart(req.params.id) })
  } catch (error) {
    respondError(res, error)
  }
}
