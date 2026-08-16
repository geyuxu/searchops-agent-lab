"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.validateQuantity = validateQuantity;
exports.calculateSubtotal = calculateSubtotal;
exports.validCheckout = validCheckout;
function validateQuantity(value) {
    const quantity = Number(value);
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 99) {
        throw new Error("quantity must be an integer from 1 to 99");
    }
    return quantity;
}
function calculateSubtotal(items) {
    return items.reduce((sum, item) => sum + Number(item.unit_price) * Number(item.quantity), 0);
}
function validCheckout(value) {
    return Boolean(value.cart_id && value.email?.includes("@") && value.shipping_address?.name &&
        value.shipping_address.line1 && value.shipping_address.city);
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiZG9tYWluLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsiLi4vLi4vLi4vLi4vc3JjL2xpYi9kb21haW4udHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7QUFBQSw0Q0FNQztBQUVELDhDQUVDO0FBRUQsc0NBU0M7QUFyQkQsU0FBZ0IsZ0JBQWdCLENBQUMsS0FBYztJQUM3QyxNQUFNLFFBQVEsR0FBRyxNQUFNLENBQUMsS0FBSyxDQUFDLENBQUE7SUFDOUIsSUFBSSxDQUFDLE1BQU0sQ0FBQyxTQUFTLENBQUMsUUFBUSxDQUFDLElBQUksUUFBUSxHQUFHLENBQUMsSUFBSSxRQUFRLEdBQUcsRUFBRSxFQUFFLENBQUM7UUFDakUsTUFBTSxJQUFJLEtBQUssQ0FBQywwQ0FBMEMsQ0FBQyxDQUFBO0lBQzdELENBQUM7SUFDRCxPQUFPLFFBQVEsQ0FBQTtBQUNqQixDQUFDO0FBRUQsU0FBZ0IsaUJBQWlCLENBQUMsS0FBaUQ7SUFDakYsT0FBTyxLQUFLLENBQUMsTUFBTSxDQUFDLENBQUMsR0FBRyxFQUFFLElBQUksRUFBRSxFQUFFLENBQUMsR0FBRyxHQUFHLE1BQU0sQ0FBQyxJQUFJLENBQUMsVUFBVSxDQUFDLEdBQUcsTUFBTSxDQUFDLElBQUksQ0FBQyxRQUFRLENBQUMsRUFBRSxDQUFDLENBQUMsQ0FBQTtBQUM5RixDQUFDO0FBRUQsU0FBZ0IsYUFBYSxDQUFDLEtBSTdCO0lBQ0MsT0FBTyxPQUFPLENBQ1osS0FBSyxDQUFDLE9BQU8sSUFBSSxLQUFLLENBQUMsS0FBSyxFQUFFLFFBQVEsQ0FBQyxHQUFHLENBQUMsSUFBSSxLQUFLLENBQUMsZ0JBQWdCLEVBQUUsSUFBSTtRQUMzRSxLQUFLLENBQUMsZ0JBQWdCLENBQUMsS0FBSyxJQUFJLEtBQUssQ0FBQyxnQkFBZ0IsQ0FBQyxJQUFJLENBQzVELENBQUE7QUFDSCxDQUFDIn0=