"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.DATA_NOTICE = exports.CommerceError = exports.pool = void 0;
exports.cart = cart;
exports.order = order;
exports.requestId = requestId;
exports.respondError = respondError;
const pg_1 = require("pg");
const domain_1 = require("./domain");
const DATA_NOTICE = "Product text: public Amazon ESCI data. Prices, inventory, users and orders: deterministic simulated data.";
exports.DATA_NOTICE = DATA_NOTICE;
const globalForPool = globalThis;
exports.pool = globalForPool.commercePool ||
    new pg_1.Pool({
        connectionString: process.env.DATABASE_URL,
        max: 8,
        idleTimeoutMillis: 30_000
    });
if (process.env.NODE_ENV !== "production")
    globalForPool.commercePool = exports.pool;
class CommerceError extends Error {
    status;
    constructor(status, message) {
        super(message);
        this.status = status;
    }
}
exports.CommerceError = CommerceError;
async function cart(id, client = exports.pool) {
    const cartResult = await client.query("SELECT id, status, currency, created_at, updated_at FROM lab_carts WHERE id = $1", [id]);
    if (!cartResult.rowCount)
        throw new CommerceError(404, "Cart not found");
    const items = await client.query(`SELECT id, product_id, title, unit_price, quantity,
            unit_price * quantity AS line_total
       FROM lab_cart_items WHERE cart_id = $1 ORDER BY title, id`, [id]);
    const subtotal = (0, domain_1.calculateSubtotal)(items.rows);
    return { ...cartResult.rows[0], items: items.rows, subtotal, total: subtotal, data_notice: DATA_NOTICE };
}
async function order(id, client = exports.pool) {
    const orderResult = await client.query("SELECT * FROM lab_orders WHERE id = $1", [id]);
    if (!orderResult.rowCount)
        throw new CommerceError(404, "Order not found");
    const items = await client.query(`SELECT id, product_id, title, unit_price, quantity,
            unit_price * quantity AS line_total
       FROM lab_order_items WHERE order_id = $1 ORDER BY title, id`, [id]);
    return { ...orderResult.rows[0], items: items.rows };
}
function requestId(headers) {
    const value = headers["x-request-id"];
    return typeof value === "string" && value ? value : crypto.randomUUID();
}
function respondError(res, error) {
    const status = error instanceof CommerceError ? error.status : 500;
    const message = error instanceof Error ? error.message : "Unknown commerce error";
    res.status(status).json({ error: status === 500 ? "internal_error" : "commerce_error", message });
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiZGIuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi8uLi8uLi9zcmMvbGliL2RiLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiI7OztBQTJCQSxvQkFjQztBQUVELHNCQVVDO0FBRUQsOEJBR0M7QUFFRCxvQ0FJQztBQWhFRCwyQkFBcUM7QUFDckMscUNBQTRDO0FBRTVDLE1BQU0sV0FBVyxHQUNmLDJHQUEyRyxDQUFBO0FBOERwRyxrQ0FBVztBQTVEcEIsTUFBTSxhQUFhLEdBQUcsVUFBZ0QsQ0FBQTtBQUV6RCxRQUFBLElBQUksR0FDZixhQUFhLENBQUMsWUFBWTtJQUMxQixJQUFJLFNBQUksQ0FBQztRQUNQLGdCQUFnQixFQUFFLE9BQU8sQ0FBQyxHQUFHLENBQUMsWUFBWTtRQUMxQyxHQUFHLEVBQUUsQ0FBQztRQUNOLGlCQUFpQixFQUFFLE1BQU07S0FDMUIsQ0FBQyxDQUFBO0FBRUosSUFBSSxPQUFPLENBQUMsR0FBRyxDQUFDLFFBQVEsS0FBSyxZQUFZO0lBQUUsYUFBYSxDQUFDLFlBQVksR0FBRyxZQUFJLENBQUE7QUFFNUUsTUFBYSxhQUFjLFNBQVEsS0FBSztJQUU3QjtJQURULFlBQ1MsTUFBYyxFQUNyQixPQUFlO1FBRWYsS0FBSyxDQUFDLE9BQU8sQ0FBQyxDQUFBO1FBSFAsV0FBTSxHQUFOLE1BQU0sQ0FBUTtJQUl2QixDQUFDO0NBQ0Y7QUFQRCxzQ0FPQztBQUVNLEtBQUssVUFBVSxJQUFJLENBQUMsRUFBVSxFQUFFLFNBQTRCLFlBQUk7SUFDckUsTUFBTSxVQUFVLEdBQUcsTUFBTSxNQUFNLENBQUMsS0FBSyxDQUNuQyxrRkFBa0YsRUFDbEYsQ0FBQyxFQUFFLENBQUMsQ0FDTCxDQUFBO0lBQ0QsSUFBSSxDQUFDLFVBQVUsQ0FBQyxRQUFRO1FBQUUsTUFBTSxJQUFJLGFBQWEsQ0FBQyxHQUFHLEVBQUUsZ0JBQWdCLENBQUMsQ0FBQTtJQUN4RSxNQUFNLEtBQUssR0FBRyxNQUFNLE1BQU0sQ0FBQyxLQUFLLENBQzlCOztpRUFFNkQsRUFDN0QsQ0FBQyxFQUFFLENBQUMsQ0FDTCxDQUFBO0lBQ0QsTUFBTSxRQUFRLEdBQUcsSUFBQSwwQkFBaUIsRUFBQyxLQUFLLENBQUMsSUFBSSxDQUFDLENBQUE7SUFDOUMsT0FBTyxFQUFFLEdBQUcsVUFBVSxDQUFDLElBQUksQ0FBQyxDQUFDLENBQUMsRUFBRSxLQUFLLEVBQUUsS0FBSyxDQUFDLElBQUksRUFBRSxRQUFRLEVBQUUsS0FBSyxFQUFFLFFBQVEsRUFBRSxXQUFXLEVBQUUsV0FBVyxFQUFFLENBQUE7QUFDMUcsQ0FBQztBQUVNLEtBQUssVUFBVSxLQUFLLENBQUMsRUFBVSxFQUFFLFNBQTRCLFlBQUk7SUFDdEUsTUFBTSxXQUFXLEdBQUcsTUFBTSxNQUFNLENBQUMsS0FBSyxDQUFDLHdDQUF3QyxFQUFFLENBQUMsRUFBRSxDQUFDLENBQUMsQ0FBQTtJQUN0RixJQUFJLENBQUMsV0FBVyxDQUFDLFFBQVE7UUFBRSxNQUFNLElBQUksYUFBYSxDQUFDLEdBQUcsRUFBRSxpQkFBaUIsQ0FBQyxDQUFBO0lBQzFFLE1BQU0sS0FBSyxHQUFHLE1BQU0sTUFBTSxDQUFDLEtBQUssQ0FDOUI7O21FQUUrRCxFQUMvRCxDQUFDLEVBQUUsQ0FBQyxDQUNMLENBQUE7SUFDRCxPQUFPLEVBQUUsR0FBRyxXQUFXLENBQUMsSUFBSSxDQUFDLENBQUMsQ0FBQyxFQUFFLEtBQUssRUFBRSxLQUFLLENBQUMsSUFBSSxFQUFFLENBQUE7QUFDdEQsQ0FBQztBQUVELFNBQWdCLFNBQVMsQ0FBQyxPQUFnQztJQUN4RCxNQUFNLEtBQUssR0FBRyxPQUFPLENBQUMsY0FBYyxDQUFDLENBQUE7SUFDckMsT0FBTyxPQUFPLEtBQUssS0FBSyxRQUFRLElBQUksS0FBSyxDQUFDLENBQUMsQ0FBQyxLQUFLLENBQUMsQ0FBQyxDQUFDLE1BQU0sQ0FBQyxVQUFVLEVBQUUsQ0FBQTtBQUN6RSxDQUFDO0FBRUQsU0FBZ0IsWUFBWSxDQUFDLEdBQW9FLEVBQUUsS0FBYztJQUMvRyxNQUFNLE1BQU0sR0FBRyxLQUFLLFlBQVksYUFBYSxDQUFDLENBQUMsQ0FBQyxLQUFLLENBQUMsTUFBTSxDQUFDLENBQUMsQ0FBQyxHQUFHLENBQUE7SUFDbEUsTUFBTSxPQUFPLEdBQUcsS0FBSyxZQUFZLEtBQUssQ0FBQyxDQUFDLENBQUMsS0FBSyxDQUFDLE9BQU8sQ0FBQyxDQUFDLENBQUMsd0JBQXdCLENBQUE7SUFDakYsR0FBRyxDQUFDLE1BQU0sQ0FBQyxNQUFNLENBQUMsQ0FBQyxJQUFJLENBQUMsRUFBRSxLQUFLLEVBQUUsTUFBTSxLQUFLLEdBQUcsQ0FBQyxDQUFDLENBQUMsZ0JBQWdCLENBQUMsQ0FBQyxDQUFDLGdCQUFnQixFQUFFLE9BQU8sRUFBRSxDQUFDLENBQUE7QUFDbkcsQ0FBQyJ9