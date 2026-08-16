"use client"

import { Button, SourceNotice } from "@searchops/ui"
import { FormEvent, useEffect, useState } from "react"
import { useRouter } from "next/navigation"
import { clearCart, getCart } from "@/lib/cart"
import type { Cart } from "@/lib/types"

export default function CheckoutPage() {
  const router = useRouter()
  const [cart, setCart] = useState<Cart | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => { getCart().then(setCart).catch(reason => setError(reason.message)) }, [])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!cart || !cart.items.length) return
    setBusy(true)
    setError("")
    const fields = new FormData(event.currentTarget)
    try {
      const response = await fetch("/api/commerce/orders", {
        method: "POST",
        headers: { "content-type": "application/json", "idempotency-key": `checkout-${cart.id}` },
        body: JSON.stringify({
          cart_id: cart.id,
          email: fields.get("email"),
          shipping_address: {
            name: fields.get("name"), line1: fields.get("line1"), city: fields.get("city"),
            postcode: fields.get("postcode"), country: "US"
          }
        })
      })
      const body = await response.json()
      if (!response.ok) throw new Error(body.message || "Checkout failed")
      clearCart()
      router.push(`/orders/${body.order.id}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Checkout failed")
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="checkout-page">
      <div className="checkout-heading"><span>02 / CHECKOUT</span><h1>A simulation,<br /><em>with real system seams.</em></h1></div>
      <div className="checkout-grid">
        <form onSubmit={submit} className="checkout-form">
          <h2>Where should it go?</h2>
          <label>Email<input required name="email" type="email" defaultValue="shopper@example.com" /></label>
          <label>Full name<input required name="name" defaultValue="Demo Shopper" /></label>
          <label>Address<input required name="line1" defaultValue="100 Search Avenue" /></label>
          <div className="field-row"><label>City<input required name="city" defaultValue="Seattle" /></label><label>Postcode<input required name="postcode" defaultValue="98101" /></label></div>
          <Button data-testid="place-order" type="submit" disabled={busy || !cart?.items.length}>{busy ? "Creating order…" : "Place simulated order"}</Button>
          {error ? <p className="inline-error">{error}</p> : null}
          <SourceNotice compact />
        </form>
        <aside className="order-summary">
          <div className="section-label"><span>ORDER STUDY</span><span>{cart?.items.length || 0} LINES</span></div>
          {!cart ? <p>Loading cart…</p> : cart.items.length === 0 ? <p>Your cart is empty. Return to the market to add a product.</p> : cart.items.map(item => (
            <div className="summary-item" key={item.id}><div><strong>{item.title}</strong><small>Qty {item.quantity} · simulated price</small></div><span>${(item.line_total / 100).toFixed(2)}</span></div>
          ))}
          <div className="summary-total"><span>Total</span><strong>${((cart?.total || 0) / 100).toFixed(2)}</strong></div>
        </aside>
      </div>
    </main>
  )
}

