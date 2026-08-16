"use client"

import { Badge, SourceNotice } from "@searchops/ui"
import Link from "next/link"
import { useParams } from "next/navigation"
import { useEffect, useState } from "react"
import type { Order } from "@/lib/types"

export default function OrderPage() {
  const params = useParams<{ id: string }>()
  const [order, setOrder] = useState<Order | null>(null)
  const [error, setError] = useState("")

  useEffect(() => {
    fetch(`/api/commerce/orders/${params.id}`, { cache: "no-store" })
      .then(async response => { const body = await response.json(); if (!response.ok) throw new Error(body.message); return body.order })
      .then(setOrder).catch(reason => setError(reason.message || "Order not found"))
  }, [params.id])

  if (error) return <main className="narrow"><div className="state-panel">{error}</div></main>
  if (!order) return <main className="narrow"><div className="state-panel">Loading order…</div></main>

  return (
    <main className="order-result" data-testid="order-result">
      <Badge tone="live">ORDER CREATED</Badge>
      <p className="order-kicker">SIMULATION CONFIRMED / #{order.display_id}</p>
      <h1>Your search<br /><em>became an order.</em></h1>
      <div className="receipt">
        <div><span>Status</span><strong>{order.status}</strong></div>
        <div><span>Email</span><strong>{order.email}</strong></div>
        <div><span>Items</span><strong>{order.items.reduce((sum, item) => sum + item.quantity, 0)}</strong></div>
        <div><span>Total</span><strong>${(order.total / 100).toFixed(2)}</strong></div>
      </div>
      <SourceNotice />
      <Link className="return-link" href="/">Continue exploring →</Link>
    </main>
  )
}

