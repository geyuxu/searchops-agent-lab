"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.POST = POST;
const db_1 = require("../../../../../../lib/db");
const domain_1 = require("../../../../../../lib/domain");
async function POST(req, res) {
    const client = await db_1.pool.connect();
    try {
        const productId = req.body?.product_id;
        let quantity;
        try {
            quantity = (0, domain_1.validateQuantity)(req.body?.quantity ?? 1);
        }
        catch (error) {
            throw new db_1.CommerceError(400, error.message);
        }
        if (!productId) {
            throw new db_1.CommerceError(400, "product_id and quantity (1-99) are required");
        }
        await client.query("BEGIN");
        const cartResult = await client.query("SELECT status FROM lab_carts WHERE id = $1 FOR UPDATE", [
            req.params.id
        ]);
        if (!cartResult.rowCount)
            throw new db_1.CommerceError(404, "Cart not found");
        if (cartResult.rows[0].status !== "active")
            throw new db_1.CommerceError(409, "Cart is completed");
        const product = await client.query("SELECT product_id, title, price_cents, inventory FROM lab_products WHERE product_id = $1", [productId]);
        if (!product.rowCount)
            throw new db_1.CommerceError(404, "Product not found in commerce catalog");
        const existing = await client.query("SELECT id, quantity FROM lab_cart_items WHERE cart_id = $1 AND product_id = $2", [req.params.id, productId]);
        const nextQuantity = quantity + (existing.rowCount ? Number(existing.rows[0].quantity) : 0);
        if (nextQuantity > Number(product.rows[0].inventory)) {
            throw new db_1.CommerceError(409, "Requested quantity exceeds simulated inventory");
        }
        await client.query(`INSERT INTO lab_cart_items (id, cart_id, product_id, title, unit_price, quantity)
       VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT (cart_id, product_id) DO UPDATE SET quantity = EXCLUDED.quantity`, [
            existing.rowCount ? existing.rows[0].id : crypto.randomUUID(),
            req.params.id,
            productId,
            product.rows[0].title,
            product.rows[0].price_cents,
            nextQuantity
        ]);
        await client.query("UPDATE lab_carts SET updated_at = now() WHERE id = $1", [req.params.id]);
        await client.query("COMMIT");
        res.setHeader("X-Request-ID", (0, db_1.requestId)(req.headers));
        res.status(201).json({ cart: await (0, db_1.cart)(req.params.id) });
    }
    catch (error) {
        await client.query("ROLLBACK");
        (0, db_1.respondError)(res, error);
    }
    finally {
        client.release();
    }
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoicm91dGUuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi8uLi8uLi8uLi8uLi8uLi8uLi8uLi9zcmMvYXBpL2xhYi9jb21tZXJjZS9jYXJ0cy9baWRdL2l0ZW1zL3JvdXRlLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiI7O0FBTUEsb0JBb0RDO0FBekRELGlEQUE2RjtBQUM3Rix5REFBK0Q7QUFJeEQsS0FBSyxVQUFVLElBQUksQ0FBQyxHQUEyQixFQUFFLEdBQW1CO0lBQ3pFLE1BQU0sTUFBTSxHQUFHLE1BQU0sU0FBSSxDQUFDLE9BQU8sRUFBRSxDQUFBO0lBQ25DLElBQUksQ0FBQztRQUNILE1BQU0sU0FBUyxHQUFHLEdBQUcsQ0FBQyxJQUFJLEVBQUUsVUFBVSxDQUFBO1FBQ3RDLElBQUksUUFBZ0IsQ0FBQTtRQUNwQixJQUFJLENBQUM7WUFBQyxRQUFRLEdBQUcsSUFBQSx5QkFBZ0IsRUFBQyxHQUFHLENBQUMsSUFBSSxFQUFFLFFBQVEsSUFBSSxDQUFDLENBQUMsQ0FBQTtRQUFDLENBQUM7UUFDNUQsT0FBTyxLQUFLLEVBQUUsQ0FBQztZQUFDLE1BQU0sSUFBSSxrQkFBYSxDQUFDLEdBQUcsRUFBRyxLQUFlLENBQUMsT0FBTyxDQUFDLENBQUE7UUFBQyxDQUFDO1FBQ3hFLElBQUksQ0FBQyxTQUFTLEVBQUUsQ0FBQztZQUNmLE1BQU0sSUFBSSxrQkFBYSxDQUFDLEdBQUcsRUFBRSw2Q0FBNkMsQ0FBQyxDQUFBO1FBQzdFLENBQUM7UUFDRCxNQUFNLE1BQU0sQ0FBQyxLQUFLLENBQUMsT0FBTyxDQUFDLENBQUE7UUFDM0IsTUFBTSxVQUFVLEdBQUcsTUFBTSxNQUFNLENBQUMsS0FBSyxDQUFDLHVEQUF1RCxFQUFFO1lBQzdGLEdBQUcsQ0FBQyxNQUFNLENBQUMsRUFBRTtTQUNkLENBQUMsQ0FBQTtRQUNGLElBQUksQ0FBQyxVQUFVLENBQUMsUUFBUTtZQUFFLE1BQU0sSUFBSSxrQkFBYSxDQUFDLEdBQUcsRUFBRSxnQkFBZ0IsQ0FB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