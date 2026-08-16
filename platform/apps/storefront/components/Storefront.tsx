"use client"

import { Badge, SourceNotice } from "@searchops/ui"
import Link from "next/link"
import { FormEvent, useCallback, useEffect, useState } from "react"
import { getCart } from "@/lib/cart"
import type { SearchResponse } from "@/lib/types"
import { ProductCard } from "./ProductCard"

export function Storefront() {
  const [draft, setDraft] = useState("")
  const [query, setQuery] = useState("")
  const [brand, setBrand] = useState("")
  const [category, setCategory] = useState("")
  const [sort, setSort] = useState("relevance")
  const [data, setData] = useState<SearchResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [cartCount, setCartCount] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    const params = new URLSearchParams({ q: query, size: "24", sort })
    if (brand) params.set("brand", brand)
    if (category) params.set("category", category)
    try {
      const response = await fetch(`/api/search?${params}`, { cache: "no-store" })
      const body = await response.json()
      if (!response.ok) throw new Error(body.message || "Search is not ready")
      setData(body)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Search failed")
    } finally {
      setLoading(false)
    }
  }, [query, brand, category, sort])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    getCart().then(cart => setCartCount(cart.items.reduce((sum, item) => sum + item.quantity, 0))).catch(() => undefined)
  }, [])

  function submit(event: FormEvent) {
    event.preventDefault()
    setBrand("")
    setCategory("")
    setQuery(draft.trim())
  }

  return (
    <main>
      <section className="hero">
        <div>
          <Badge tone="live">BM25 · LIVE INDEX</Badge>
          <h1>Find what you mean.<br /><em>See why it ranked.</em></h1>
          <p>A working market on top of a public relevance benchmark—every result is ready for inspection, testing and governed change.</p>
        </div>
        <div className="hero__aside">
          <span>01 / DISCOVERY</span>
          <strong>{data ? data.total.toLocaleString() : "—"}</strong>
          <small>matching products · strategy v{data?.strategy_version ?? "—"}</small>
        </div>
      </section>

      <section className="search-deck" aria-label="Product search">
        <form onSubmit={submit}>
          <label htmlFor="query">WHAT ARE YOU LOOKING FOR?</label>
          <div className="search-input-row">
            <input id="query" data-testid="search-input" value={draft} onChange={event => setDraft(event.target.value)} placeholder="Try: wireless headphones" />
            <button data-testid="search-submit" type="submit">SEARCH <span>↗</span></button>
          </div>
        </form>
        <Link className="cart-link" href="/checkout" data-testid="cart-link">CART <span>{cartCount.toString().padStart(2, "0")}</span></Link>
      </section>

      <SourceNotice />

      <section className="catalog-layout">
        <aside className="filters">
          <div className="section-label"><span>FILTERS</span><button onClick={() => { setBrand(""); setCategory(""); }}>CLEAR</button></div>
          <label>Brand
            <select value={brand} onChange={event => setBrand(event.target.value)}>
              <option value="">All brands</option>
              {data?.facets.brands.map(item => <option key={item.key} value={item.key}>{item.key} ({item.count})</option>)}
            </select>
          </label>
          <label>Category
            <select value={category} onChange={event => setCategory(event.target.value)}>
              <option value="">All categories</option>
              {data?.facets.categories.map(item => <option key={item.key} value={item.key}>{item.key} ({item.count})</option>)}
            </select>
          </label>
          <div className="lab-note">
            <span>SEARCH TRACE</span>
            <code>{data?.request_id.slice(0, 12) || "waiting"}</code>
            <small>{data ? `${data.latency_ms} ms · “${data.effective_query || "match all"}”` : "No request yet"}</small>
          </div>
        </aside>

        <div className="catalog">
          <div className="catalog__header">
            <div><span>CURATED BY THE QUERY</span><h2>{query ? `Results for “${query}”` : "The open shelf"}</h2></div>
            <label>Sort
              <select value={sort} onChange={event => setSort(event.target.value)}>
                <option value="relevance">Relevance</option>
                <option value="price_asc">Price, low first</option>
                <option value="price_desc">Price, high first</option>
                <option value="popularity">Simulated popularity</option>
              </select>
            </label>
          </div>
          {error ? <div className="state-panel"><strong>Search service is warming up.</strong><p>{error}</p><button onClick={load}>Try again</button></div> : null}
          {loading ? <div className="product-grid" aria-label="Loading products">{Array.from({ length: 8 }).map((_, index) => <div className="product-skeleton" key={index} />)}</div> : null}
          {!loading && !error && data?.products.length === 0 ? <div className="state-panel"><strong>No products found.</strong><p>Try a broader phrase or clear the filters. This zero-result query was recorded for SearchOps.</p></div> : null}
          {!loading && data ? <div className="product-grid">{data.products.map(product => <ProductCard key={product.product_id} product={product} onCartChange={setCartCount} />)}</div> : null}
        </div>
      </section>
    </main>
  )
}

