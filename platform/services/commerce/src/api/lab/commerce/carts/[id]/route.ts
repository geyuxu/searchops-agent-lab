import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { cart, requestId, respondError } from "../../../../../lib/db"

export async function GET(req: MedusaRequest, res: MedusaResponse) {
  try {
    res.setHeader("X-Request-ID", requestId(req.headers))
    res.json({ cart: await cart(req.params.id) })
  } catch (error) {
    respondError(res, error)
  }
}

