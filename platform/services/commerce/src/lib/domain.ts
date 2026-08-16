export function validateQuantity(value: unknown): number {
  const quantity = Number(value)
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > 99) {
    throw new Error("quantity must be an integer from 1 to 99")
  }
  return quantity
}

export function calculateSubtotal(items: { unit_price: number; quantity: number }[]): number {
  return items.reduce((sum, item) => sum + Number(item.unit_price) * Number(item.quantity), 0)
}

export function validCheckout(value: {
  cart_id?: string
  email?: string
  shipping_address?: { name?: string; line1?: string; city?: string }
}): boolean {
  return Boolean(
    value.cart_id && value.email?.includes("@") && value.shipping_address?.name &&
    value.shipping_address.line1 && value.shipping_address.city
  )
}

