"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.PATCH = PATCH;
exports.DELETE = DELETE;
const db_1 = require("../../../../../../../lib/db");
const domain_1 = require("../../../../../../../lib/domain");
async function PATCH(req, res) {
    try {
        let quantity;
        try {
            quantity = (0, domain_1.validateQuantity)(req.body?.quantity);
        }
        catch (error) {
            throw new db_1.CommerceError(400, error.message);
        }
        const result = await db_1.pool.query(`UPDATE lab_cart_items i SET quantity = $1
       FROM lab_products p, lab_carts c
       WHERE i.id = $2 AND i.cart_id = $3 AND p.product_id = i.product_id
         AND c.id = i.cart_id AND c.status = 'active' AND $1 <= p.inventory
       RETURNING i.id`, [quantity, req.params.itemId, req.params.id]);
        if (!result.rowCount)
            throw new db_1.CommerceError(409, "Item update rejected or inventory unavailable");
        await db_1.pool.query("UPDATE lab_carts SET updated_at = now() WHERE id = $1", [req.params.id]);
        res.setHeader("X-Request-ID", (0, db_1.requestId)(req.headers));
        res.json({ cart: await (0, db_1.cart)(req.params.id) });
    }
    catch (error) {
        (0, db_1.respondError)(res, error);
    }
}
async function DELETE(req, res) {
    try {
        const result = await db_1.pool.query(`DELETE FROM lab_cart_items i USING lab_carts c
       WHERE i.id = $1 AND i.cart_id = $2 AND c.id = i.cart_id AND c.status = 'active'
       RETURNING i.id`, [req.params.itemId, req.params.id]);
        if (!result.rowCount)
            throw new db_1.CommerceError(404, "Active cart item not found");
        res.setHeader("X-Request-ID", (0, db_1.requestId)(req.headers));
        res.json({ cart: await (0, db_1.cart)(req.params.id) });
    }
    catch (error) {
        (0, db_1.respondError)(res, error);
    }
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoicm91dGUuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi8uLi8uLi8uLi8uLi8uLi8uLi8uLi8uLi9zcmMvYXBpL2xhYi9jb21tZXJjZS9jYXJ0cy9baWRdL2l0ZW1zL1tpdGVtSWRdL3JvdXRlLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiI7O0FBTUEsc0JBb0JDO0FBRUQsd0JBY0M7QUF6Q0Qsb0RBQWdHO0FBQ2hHLDREQUFrRTtBQUkzRCxLQUFLLFVBQVUsS0FBSyxDQUFDLEdBQThCLEVBQUUsR0FBbUI7SUFDN0UsSUFBSSxDQUFDO1FBQ0gsSUFBSSxRQUFnQixDQUFBO1FBQ3BCLElBQUksQ0FBQztZQUFDLFFBQVEsR0FBRyxJQUFBLHlCQUFnQixFQUFDLEdBQUcsQ0FBQyxJQUFJLEVBQUUsUUFBUSxDQUFDLENBQUE7UUFBQyxDQUFDO1FBQ3ZELE9BQU8sS0FBSyxFQUFFLENBQUM7WUFBQyxNQUFNLElBQUksa0JBQWEsQ0FBQyxHQUFHLEVBQUcsS0FBZSxDQUFDLE9BQU8sQ0FBQyxDQUFBO1FBQUMsQ0FBQztRQUN4RSxNQUFNLE1BQU0sR0FBRyxNQUFNLFNBQUksQ0FBQyxLQUFLLENBQzdCOzs7O3NCQUlnQixFQUNoQixDQUFDLFFBQVEsRUFBRSxHQUFHLENBQUMsTUFBTSxDQUFDLE1BQU0sRUFBRSxHQUFHLENBQUMsTUFBTSxDQUFDLEVBQUUsQ0FBQyxDQUM3QyxDQUFBO1FBQ0QsSUFBSSxDQUFDLE1BQU0sQ0FBQyxRQUFRO1lBQUUsTUFBTSxJQUFJLGtCQUFhLENBQUMsR0FBRyxFQUFFLCtDQUErQyxDQUFDLENBQUE7UUFDbkcsTUFBTSxTQUFJLENBQUMsS0FBSyxDQUFDLHVEQUF1RCxFQUFFLENBQUMsR0FBRyxDQUFDLE1BQU0sQ0FBQyxFQUFFLENBQUMsQ0FBQyxDQUFBO1FBQzFGLEdBQUcsQ0FBQyxTQUFTLENBQUMsY0FBYyxFQUFFLElBQUEsY0FBUyxFQUFDLEdBQUcsQ0FBQyxPQUFPLENBQUMsQ0FBQyxDQUFBO1FBQ3JELEdBQUcsQ0FBQyxJQUFJLENBQUMsRUFBRSxJQUFJLEVBQUUsTUFBTSxJQUFBLFNBQUksRUFBQyxHQUFHLENBQUMsTUFBTSxDQUFDLEVBQUUsQ0FBQyxFQUFFLENBQUMsQ0FBQTtJQUMvQyxDQUFDO0lBQUMsT0FBTyxLQUFLLEVBQUUsQ0FBQztRQUNmLElBQUEsaUJBQVksRUFBQyxHQUFHLEVBQUUsS0FBSyxDQUFDLENBQUE7SUFDMUIsQ0FBQztBQUNILENBQUM7QUFFTSxLQUFLLFVBQVUsTUFBTSxDQUFDLEdBQWtCLEVBQUUsR0FBbUI7SUFDbEUsSUFBSSxDQUFDO1FBQ0gsTUFBTSxNQUFNLEdBQUcsTUFBTSxTQUFJLENBQUMsS0FBSyxDQUM3Qjs7c0JBRWdCLEVBQ2hCLENBQUMsR0FBRyxDQUFDLE1BQU0sQ0FBQyxNQUFNLEVBQUUsR0FBRyxDQUFDLE1BQU0sQ0FBQyxFQUFFLENBQUMsQ0FDbkMsQ0FBQTtRQUNELElBQUksQ0FBQyxNQUFNLENBQUMsUUFBUTtZQUFFLE1BQU0sSUFBSSxrQkFBYSxDQUFDLEdBQUcsRUFBRSw0QkFBNEIsQ0FBQyxDQUFBO1FBQ2hGLEdBQUcsQ0FBQyxTQUFTLENBQUMsY0FBYyxFQUFFLElBQUEsY0FBUyxFQUFDLEdBQUcsQ0FBQyxPQUFPLENBQUMsQ0FBQyxDQUFBO1FBQ3JELEdBQUcsQ0FBQyxJQUFJLENBQUMsRUFBRSxJQUFJLEVBQUUsTUFBTSxJQUFBLFNBQUksRUFBQyxHQUFHLENBQUMsTUFBTSxDQUFDLEVBQUUsQ0FBQyxFQUFFLENBQUMsQ0FBQTtJQUMvQyxDQUFDO0lBQUMsT0FBTyxLQUFLLEVBQUUsQ0FBQztRQUNmLElBQUEsaUJBQVksRUFBQyxHQUFHLEVBQUUsS0FBSyxDQUFDLENBQUE7SUFDMUIsQ0FBQztBQUNILENBQUMifQ==