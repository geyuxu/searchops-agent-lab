"use client"

import { Badge, Button, SourceNotice } from "@searchops/ui"
import Link from "next/link"
import { useParams, useRouter } from "next/navigation"
import { useEffect, useState } from "react"
import { ProductArt } from "@/components/ProductArt"
import { addToCart } from "@/lib/cart"
import type { Product } from "@/lib/types"

export default function ProductPage() {
  const params = useParams<{ id: string }>()
  const router = useRouter()
  const [product, setProduct] = useState<Product | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    fetch(`/api/products/${encodeURIComponent(params.id)}`, { cache: "no-store" })
      .then(async response => {
        if (!response.ok) throw new Error("Product not found")
        return response.json()
      })
      .then(setProduct)
      .catch(reason => setError(reason.message))
  }, [params.id])

  async function add(goToCheckout: boolean) {
    if (!product) return
    setBusy(true)
    try {
      await addToCart(product.product_id)
      if (goToCheckout) router.push("/checkout")
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not add item")
    } finally {
      setBusy(false)
    }
  }

  if (error && !product) return <main className="narrow"><div className="state-panel"><strong>{error}</strong><Link href="/">Back to search</Link></div></main>
  if (!product) return <main className="narrow"><div className="state-panel">Loading product…</div></main>

  return (
    <main className="product-page" data-testid="product-detail">
      <div className="breadcrumbs"><Link href="/">Market</Link><span>/</span><span>{product.category}</span></div>
      <section className="product-detail">
        <ProductArt hue={product.placeholder_hue} label={product.brand || product.title} />
        <div className="product-detail__copy">
          <div className="eyebrow-row"><Badge tone="neutral">ESCI PRODUCT TEXT</Badge><span>ID {product.product_id}</span></div>
          <h1>{product.title}</h1>
          <p className="product-brand">{product.brand || "Unbranded"} · {product.color || "Colour unspecified"}</p>
          <p className="product-description">{product.description || product.bullet_point || "No description was supplied in the public dataset."}</p>
          {product.bullet_point ? <div className="bullet-copy">{product.bullet_point}</div> : null}
          <div className="buy-panel">
            <strong>${(product.price_cents / 100).toFixed(2)}</strong>
            <span>{product.inventory} units · simulated inventory</span>
            <Button data-testid="detail-add-to-cart" disabled={busy || product.inventory < 1} onClick={() => add(false)}>{busy ? "Adding…" : "Add to cart"}</Button>
            <button data-testid="buy-now" className="text-button" onClick={() => add(true)}>Buy now →</button>
          </div>
          {error ? <p className="inline-error">{error}</p> : null}
          <SourceNotice compact />
        </div>
      </section>
    </main>
  )
}

