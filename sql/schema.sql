PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  base_url TEXT
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  brand TEXT,
  size_text TEXT,
  quality_tier TEXT,
  origin_country TEXT,
  preferred_store_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(canonical_name, brand, size_text),
  FOREIGN KEY(preferred_store_id) REFERENCES stores(id)
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL,
  external_order_id TEXT,
  ordered_at TEXT NOT NULL,
  currency TEXT DEFAULT 'AED',
  subtotal REAL,
  delivery_fee REAL,
  total REAL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(store_id, external_order_id),
  FOREIGN KEY(store_id) REFERENCES stores(id)
);

CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL,
  product_id INTEGER,
  raw_product_name TEXT NOT NULL,
  quantity REAL NOT NULL,
  unit_price REAL,
  line_total REAL,
  product_url TEXT,
  category TEXT,
  source_brand TEXT,
  source_size_text TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(order_id, raw_product_name, quantity, unit_price, line_total),
  FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
  FOREIGN KEY(product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS consumption_stats (
  product_id INTEGER PRIMARY KEY,
  avg_days_between_orders REAL,
  avg_quantity_per_order REAL,
  predicted_next_restock_at TEXT,
  confidence REAL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS product_cache (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL,
  query_text TEXT NOT NULL,
  product_title TEXT NOT NULL,
  price REAL,
  currency TEXT DEFAULT 'AED',
  size_text TEXT,
  brand TEXT,
  origin_country TEXT,
  quality_tier TEXT,
  availability_status TEXT,
  image_url TEXT,
  product_url TEXT,
  fetched_at TEXT NOT NULL,
  expires_at TEXT,
  FOREIGN KEY(store_id) REFERENCES stores(id)
);

CREATE TABLE IF NOT EXISTS product_signals (
  product_id INTEGER PRIMARY KEY,
  category_path TEXT NOT NULL,
  quality_tier TEXT NOT NULL,
  health_profile TEXT NOT NULL,
  brand_tier TEXT,
  signal_confidence REAL NOT NULL DEFAULT 0.5,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS brand_preferences (
  id INTEGER PRIMARY KEY,
  user_scope TEXT NOT NULL DEFAULT 'driver',
  category_path TEXT NOT NULL,
  brand TEXT NOT NULL,
  preference_label TEXT NOT NULL,
  preference_score REAL NOT NULL,
  order_count INTEGER NOT NULL,
  last_seen TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_scope, category_path, brand, preference_label)
);

CREATE TABLE IF NOT EXISTS quality_preferences (
  id INTEGER PRIMARY KEY,
  user_scope TEXT NOT NULL DEFAULT 'driver',
  category_path TEXT NOT NULL,
  quality_tier TEXT NOT NULL,
  health_profile TEXT NOT NULL,
  preference_score REAL NOT NULL,
  order_count INTEGER NOT NULL,
  last_seen TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_scope, category_path, quality_tier, health_profile)
);

CREATE TABLE IF NOT EXISTS seasonal_availability (
  id INTEGER PRIMARY KEY,
  category_path TEXT NOT NULL,
  product_keyword TEXT NOT NULL,
  origin_country TEXT,
  season_start_month INTEGER NOT NULL,
  season_end_month INTEGER NOT NULL,
  peak_months TEXT,
  availability_confidence REAL NOT NULL DEFAULT 0.4,
  source TEXT NOT NULL DEFAULT 'seeded_knowledge',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(category_path, product_keyword, origin_country)
);

CREATE TABLE IF NOT EXISTS suggestion_events (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  context_category TEXT NOT NULL,
  requested_item TEXT,
  usual_brand TEXT,
  preferred_quality_tier TEXT,
  suggested_item TEXT,
  suggested_brand TEXT,
  suggested_quality_tier TEXT,
  reasoning TEXT NOT NULL,
  user_response TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_store_ordered_at ON orders(store_id, ordered_at);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_cache_store_query ON product_cache(store_id, query_text);
CREATE INDEX IF NOT EXISTS idx_brand_preferences_category ON brand_preferences(user_scope, category_path);
CREATE INDEX IF NOT EXISTS idx_quality_preferences_category ON quality_preferences(user_scope, category_path);
