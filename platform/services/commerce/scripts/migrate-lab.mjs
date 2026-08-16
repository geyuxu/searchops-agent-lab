import pg from "pg"

const { Client } = pg
const client = new Client({ connectionString: process.env.DATABASE_URL })

const migration = `
CREATE TABLE IF NOT EXISTS lab_products (
  product_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  brand TEXT NOT NULL DEFAULT '',
  price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
  currency TEXT NOT NULL DEFAULT 'USD',
  inventory INTEGER NOT NULL CHECK (inventory >= 0),
  provenance TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lab_carts (
  id UUID PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed')),
  currency TEXT NOT NULL DEFAULT 'USD',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lab_cart_items (
  id UUID PRIMARY KEY,
  cart_id UUID NOT NULL REFERENCES lab_carts(id) ON DELETE CASCADE,
  product_id TEXT NOT NULL REFERENCES lab_products(product_id),
  title TEXT NOT NULL,
  unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
  quantity INTEGER NOT NULL CHECK (quantity > 0 AND quantity <= 99),
  UNIQUE (cart_id, product_id)
);

CREATE SEQUENCE IF NOT EXISTS lab_order_display_id_seq START WITH 1001;

CREATE TABLE IF NOT EXISTS lab_orders (
  id UUID PRIMARY KEY,
  display_id BIGINT NOT NULL DEFAULT nextval('lab_order_display_id_seq'),
  cart_id UUID NOT NULL UNIQUE REFERENCES lab_carts(id),
  email TEXT NOT NULL,
  subtotal INTEGER NOT NULL,
  shipping INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD',
  status TEXT NOT NULL DEFAULT 'placed',
  shipping_address JSONB NOT NULL,
  data_notice TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lab_order_items (
  id UUID PRIMARY KEY,
  order_id UUID NOT NULL REFERENCES lab_orders(id) ON DELETE CASCADE,
  product_id TEXT NOT NULL,
  title TEXT NOT NULL,
  unit_price INTEGER NOT NULL,
  quantity INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_order_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  order_id UUID NOT NULL REFERENCES lab_orders(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
`

await client.connect()
try {
  await client.query(migration)
  process.stdout.write(JSON.stringify({ event: "commerce.migration.complete", status: "ok" }) + "\n")
} finally {
  await client.end()
}

