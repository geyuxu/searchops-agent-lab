import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { order, requestId, respondError } from "../../../../../lib/db"

export async function GET(req: MedusaRequest, res: MedusaResponse) {
  try {
    res.setHeader("X-Request-ID", requestId(req.headers))
    res.json({ order: await order(req.params.id) })
  } catch (error) {
    respondError(res, error)
  }
}

