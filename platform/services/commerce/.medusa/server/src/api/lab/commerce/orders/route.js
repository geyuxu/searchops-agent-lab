"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.POST = POST;
const db_1 = require("../../../../lib/db");
const domain_1 = require("../../../../lib/domain");
async function POST(req, res) {
    const client = await db_1.pool.connect();
    try {
        const { cart_id: cartId, email, shipping_address: address } = req.body || {};
        const idempotencyKey = String(req.headers["idempotency-key"] || "");
        if (!(0, domain_1.validCheckout)({ cart_id: cartId, email, shipping_address: address })) {
            throw new db_1.CommerceError(400, "cart_id, a valid email and complete shipping_address are required");
        }
        if (!idempotencyKey || idempotencyKey.length > 200) {
            throw new db_1.CommerceError(400, "Idempotency-Key header is required");
        }
        await client.query("BEGIN");
        const replay = await client.query("SELECT order_id FROM lab_order_idempotency WHERE idempotency_key = $1", [idempotencyKey]);
        if (replay.rowCount) {
            await client.query("COMMIT");
            res.setHeader("X-Request-ID", (0, db_1.requestId)(req.headers));
            return res.json({ order: await (0, db_1.order)(replay.rows[0].order_id) });
        }
        const cartState = await client.query("SELECT status FROM lab_carts WHERE id = $1 FOR UPDATE", [cartId]);
        if (!cartState.rowCount)
            throw new db_1.CommerceError(404, "Cart not found");
        if (cartState.rows[0].status !== "active") {
            const existing = await client.query("SELECT id FROM lab_orders WHERE cart_id = $1", [cartId]);
            if (existing.rowCount) {
                await client.query("COMMIT");
                return res.json({ order: await (0, db_1.order)(existing.rows[0].id) });
            }
            throw new db_1.CommerceError(409, "Cart is already completed");
        }
        const snapshot = await (0, db_1.cart)(cartId, client);
        if (!snapshot.items.length)
            throw new db_1.CommerceError(400, "Cannot place an empty cart");
        for (const item of snapshot.items) {
            const inventory = await client.query("UPDATE lab_products SET inventory = inventory - $1 WHERE product_id = $2 AND inventory >= $1 RETURNING product_id", [item.quantity, item.product_id]);
            if (!inventory.rowCount)
                throw new db_1.CommerceError(409, `Inventory changed for ${item.product_id}`);
        }
        const orderId = crypto.randomUUID();
        await client.query(`INSERT INTO lab_orders
       (id, cart_id, email, subtotal, shipping, total, currency, shipping_address, data_notice)
       VALUES ($1, $2, $3, $4, 0, $4, $5, $6::jsonb, $7)`, [orderId, cartId, email, snapshot.subtotal, snapshot.currency, JSON.stringify(address), db_1.DATA_NOTICE]);
        for (const item of snapshot.items) {
            await client.query(`INSERT INTO lab_order_items (id, order_id, product_id, title, unit_price, quantity)
         VALUES ($1, $2, $3, $4, $5, $6)`, [crypto.randomUUID(), orderId, item.product_id, item.title, item.unit_price, item.quantity]);
        }
        await client.query("UPDATE lab_carts SET status = 'completed', updated_at = now() WHERE id = $1", [cartId]);
        await client.query("INSERT INTO lab_order_idempotency (idempotency_key, order_id) VALUES ($1, $2)", [idempotencyKey, orderId]);
        await client.query("COMMIT");
        res.setHeader("X-Request-ID", (0, db_1.requestId)(req.headers));
        res.status(201).json({ order: await (0, db_1.order)(orderId) });
    }
    catch (error) {
        await client.query("ROLLBACK");
        (0, db_1.respondError)(res, error);
    }
    finally {
        client.release();
    }
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoicm91dGUuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi8uLi8uLi8uLi8uLi8uLi9zcmMvYXBpL2xhYi9jb21tZXJjZS9vcmRlcnMvcm91dGUudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7QUFVQSxvQkFvRUM7QUE3RUQsMkNBQTJHO0FBQzNHLG1EQUFzRDtBQVEvQyxLQUFLLFVBQVUsSUFBSSxDQUFDLEdBQThCLEVBQUUsR0FBbUI7SUFDNUUsTUFBTSxNQUFNLEdBQUcsTUFBTSxTQUFJLENBQUMsT0FBTyxFQUFFLENBQUE7SUFDbkMsSUFBSSxDQUFDO1FBQ0gsTUFBTSxFQUFFLE9BQU8sRUFBRSxNQUFNLEVBQUUsS0FBSyxFQUFFLGdCQUFnQixFQUFFLE9BQU8sRUFBRSxHQUFHLEdBQUcsQ0FBQyxJQUFJLElBQUksRUFBRSxDQUFBO1FBQzVFLE1BQU0sY0FBYyxHQUFHLE1BQU0sQ0FBQyxHQUFHLENBQUMsT0FBTyxDQUFDLGlCQUFpQixDQUFDLElBQUksRUFBRSxDQUFDLENBQUE7UUFDbkUsSUFBSSxDQUFDLElBQUEsc0JBQWEsRUFBQyxFQUFFLE9BQU8sRUFBRSxNQUFNLEVBQUUsS0FBSyxFQUFFLGdCQUFnQixFQUFFLE9BQU8sRUFBRSxDQUFDLEVBQUUsQ0FBQztZQUMxRSxNQUFNLElBQUksa0JBQWEsQ0FBQyxHQUFHLEVBQUUsbUVBQW1FLENBQUMsQ0FBQTtRQUNuRyxDQUFDO1FBQ0QsSUFBSSxDQUFDLGNBQWMsSUFBSSxjQUFjLENBQUMsTUFBTSxHQUFHLEdBQUcsRUFBRSxDQUFDO1lBQ25ELE1BQU0sSUFBSSxrQkFBYSxDQUFDLEdBQUcsRUFBRSxvQ0FBb0MsQ0FBQyxDQUFBO1FBQ3BFLENBQUM7UUFDRCxNQUFNLE1BQU0sQ0FBQyxLQUFLLENBQUMsT0FBTyxDQUFDLENBQUE7UUFDM0IsTUFBTSxNQUFNLEdBQUcsTUFBTSxNQUFNLENBQUMsS0FBSyxDQUMvQix1RUFBdUUsRUFDdkUsQ0FBQyxjQUFjLENBQUMsQ0FDakIsQ0FB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