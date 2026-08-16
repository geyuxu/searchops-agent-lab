export type Product = {
  product_id: string
  title: string
  brand: string
  description: string
  bullet_point: string
  color: string
  category: string
  price_cents: number
  currency: string
  inventory: number
  placeholder_hue: number
  provenance: string
  score?: number
}

export type SearchResponse = {
  request_id: string
  original_query: string
  effective_query: string
  total: number
  page: number
  size: number
  latency_ms: number
  strategy_version: number
  products: Product[]
  facets: { brands: { key: string; count: number }[]; categories: { key: string; count: number }[] }
  ai_applied: boolean
  data_notice: string
}

export type CartItem = {
  id: string
  product_id: string
  title: string
  unit_price: number
  quantity: number
  line_total: number
}

export type Cart = {
  id: string
  status: string
  currency: string
  items: CartItem[]
  subtotal: number
  total: number
  data_notice: string
}

export type Order = {
  id: string
  display_id: number
  email: string
  subtotal: number
  total: number
  currency: string
  status: string
  shipping_address: Record<string, string>
  data_notice: string
  created_at: string
  items: CartItem[]
}

