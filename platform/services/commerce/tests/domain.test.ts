import assert from "node:assert/strict"
import test from "node:test"
import { calculateSubtotal, validCheckout, validateQuantity } from "../src/lib/domain.ts"

test("quantity validation enforces the cart invariant", () => {
  assert.equal(validateQuantity("2"), 2)
  assert.throws(() => validateQuantity(0), /1 to 99/)
  assert.throws(() => validateQuantity(100), /1 to 99/)
  assert.throws(() => validateQuantity(1.5), /1 to 99/)
})

test("subtotal uses integer cents", () => {
  assert.equal(calculateSubtotal([{ unit_price: 1099, quantity: 2 }, { unit_price: 500, quantity: 1 }]), 2698)
})

test("checkout requires cart, email and a minimal address", () => {
  assert.equal(validCheckout({ cart_id: "cart", email: "a@example.com", shipping_address: { name: "A", line1: "1 Main", city: "Seattle" } }), true)
  assert.equal(validCheckout({ cart_id: "cart", email: "invalid", shipping_address: { name: "A", line1: "1 Main", city: "Seattle" } }), false)
})

