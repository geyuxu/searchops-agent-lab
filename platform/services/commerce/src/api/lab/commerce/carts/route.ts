import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { cart, pool, requestId, respondError } from "../../../../lib/db"

export async function POST(req: MedusaRequest, res: MedusaResponse) {
  try {
    const id = crypto.randomUUID()
    await pool.query("INSERT INTO lab_carts (id) VALUES ($1)", [id])
    res.setHeader("X-Request-ID", requestId(req.headers))
    res.status(201).json({ cart: await cart(id) })
  } catch (error) {
    respondError(res, error)
  }
}

