"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.GET = GET;
const db_1 = require("../../../../../lib/db");
async function GET(req, res) {
    try {
        res.setHeader("X-Request-ID", (0, db_1.requestId)(req.headers));
        res.json({ cart: await (0, db_1.cart)(req.params.id) });
    }
    catch (error) {
        (0, db_1.respondError)(res, error);
    }
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoicm91dGUuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi8uLi8uLi8uLi8uLi8uLi8uLi9zcmMvYXBpL2xhYi9jb21tZXJjZS9jYXJ0cy9baWRdL3JvdXRlLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiI7O0FBR0Esa0JBT0M7QUFURCw4Q0FBcUU7QUFFOUQsS0FBSyxVQUFVLEdBQUcsQ0FBQyxHQUFrQixFQUFFLEdBQW1CO0lBQy9ELElBQUksQ0FBQztRQUNILEdBQUcsQ0FBQyxTQUFTLENBQUMsY0FBYyxFQUFFLElBQUEsY0FBUyxFQUFDLEdBQUcsQ0FBQyxPQUFPLENBQUMsQ0FBQyxDQUFBO1FBQ3JELEdBQUcsQ0FBQyxJQUFJLENBQUMsRUFBRSxJQUFJLEVBQUUsTUFBTSxJQUFBLFNBQUksRUFBQyxHQUFHLENBQUMsTUFBTSxDQUFDLEVBQUUsQ0FBQyxFQUFFLENBQUMsQ0FBQTtJQUMvQyxDQUFDO0lBQUMsT0FBTyxLQUFLLEVBQUUsQ0FBQztRQUNmLElBQUEsaUJBQVksRUFBQyxHQUFHLEVBQUUsS0FBSyxDQUFDLENBQUE7SUFDMUIsQ0FBQztBQUNILENBQUMifQ==