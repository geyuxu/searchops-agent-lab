"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.GET = GET;
const db_1 = require("../../../../../lib/db");
async function GET(req, res) {
    try {
        res.setHeader("X-Request-ID", (0, db_1.requestId)(req.headers));
        res.json({ order: await (0, db_1.order)(req.params.id) });
    }
    catch (error) {
        (0, db_1.respondError)(res, error);
    }
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoicm91dGUuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi8uLi8uLi8uLi8uLi8uLi8uLi9zcmMvYXBpL2xhYi9jb21tZXJjZS9vcmRlcnMvW2lkXS9yb3V0ZS50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOztBQUdBLGtCQU9DO0FBVEQsOENBQXNFO0FBRS9ELEtBQUssVUFBVSxHQUFHLENBQUMsR0FBa0IsRUFBRSxHQUFtQjtJQUMvRCxJQUFJLENBQUM7UUFDSCxHQUFHLENBQUMsU0FBUyxDQUFDLGNBQWMsRUFBRSxJQUFBLGNBQVMsRUFBQyxHQUFHLENBQUMsT0FBTyxDQUFDLENBQUMsQ0FBQTtRQUNyRCxHQUFHLENBQUMsSUFBSSxDQUFDLEVBQUUsS0FBSyxFQUFFLE1BQU0sSUFBQSxVQUFLLEVBQUMsR0FBRyxDQUFDLE1BQU0sQ0FBQyxFQUFFLENBQUMsRUFBRSxDQUFDLENBQUE7SUFDakQsQ0FBQztJQUFDLE9BQU8sS0FBSyxFQUFFLENBQUM7UUFDZixJQUFBLGlCQUFZLEVBQUMsR0FBRyxFQUFFLEtBQUssQ0FBQyxDQUFBO0lBQzFCLENBQUM7QUFDSCxDQUFDIn0=