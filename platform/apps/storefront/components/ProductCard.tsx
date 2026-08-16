"use client"

import { Badge, Button } from "@searchops/ui"
import Link from "next/link"
import { useState } from "react"
import { addToCart } from "@/lib/cart"
import type { Product } from "@/lib/types"
import { ProductArt } from "./ProductArt"

export function ProductCard({ product, onCartChange }: { product: Product; onCartChange: (count: number) => void }) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState("")

  async function add() {
    setBusy(true)
    setMessage("")
    try {
      const cart = await addToCart(product.product_id)
      onCartChange(cart.items.reduce((sum, item) => sum + item.quantity, 0))
      setMessage("Added")
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not add")
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className="product-card" data-testid="product-card">
      <Link href={`/products/${encodeURIComponent(product.product_id)}`} aria-label={`Open ${product.title}`}>
        <ProductArt hue={product.placeholder_hue} label={product.brand || product.title} />
      </Link>
      <div className="product-card__meta">
        <div className="eyebrow-row">
          <span>{product.brand || "Unbranded"}</span>
          <Badge tone={product.inventory > 0 ? "live" : "warn"}>{product.inventory > 0 ? `${product.inventory} in lab stock` : "Out"}</Badge>
        </div>
        <Link className="product-title" href={`/products/${encodeURIComponent(product.product_id)}`}>{product.title}</Link>
        <div className="product-card__footer">
          <strong>${(product.price_cents / 100).toFixed(2)}</strong>
          <Button data-testid="add-to-cart" disabled={busy || product.inventory < 1} onClick={add}>
            {busy ? "Adding…" : message || "Add +"}
          </Button>
        </div>
      </div>
    </article>
  )
}

