-- TRILUX/BMEcat -> SQLite schema
-- Source format: BMEcat 2005 / ETIM (etim-international.com/bmecat/50)

-- Brands, discovered from the brands/<code>/ directories
DROP TABLE IF EXISTS brand;
CREATE TABLE brand (
  code  TEXT PRIMARY KEY,   -- directory name: trilux, zumtobel ...
  name  TEXT NOT NULL,      -- display name
  colour TEXT,
  site  TEXT,
  note  TEXT,
  sort_order INTEGER DEFAULT 0
);

-- Catalogue metadata is per brand (each brand has its own version and dates)
DROP TABLE IF EXISTS catalog_info;
CREATE TABLE catalog_info (
  brand TEXT NOT NULL DEFAULT '',
  key   TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (brand, key)
);

DROP TABLE IF EXISTS product;
CREATE TABLE product (
  id                INTEGER PRIMARY KEY,
  brand             TEXT NOT NULL DEFAULT 'trilux',
  supplier_pid      TEXT NOT NULL,          -- manufacturer article no (6000868600)
  alt_pid           TEXT,                   -- internal/legacy article no
  gtin              TEXT,                   -- EAN
  manufacturer_pid  TEXT,
  manufacturer_name TEXT,
  type_descr_de     TEXT,
  type_descr_en     TEXT,
  short_de          TEXT,
  short_en          TEXT,
  long_de           TEXT,
  long_en           TEXT,
  tender_de         TEXT,
  tender_en         TEXT,
  series_de         TEXT,
  series_en         TEXT,
  variation_de      TEXT,
  variation_en      TEXT,
  keywords_de       TEXT,
  keywords_en       TEXT,
  status            TEXT,
  product_type      TEXT,
  etim_system       TEXT,
  etim_class        TEXT,                   -- EC002557
  order_unit        TEXT,
  content_unit      TEXT,
  no_cu_per_ou      TEXT,
  price_quantity    TEXT,
  quantity_min      TEXT,
  quantity_interval TEXT,
  price_amount      REAL,                   -- gross list price (see README)
  price_currency    TEXT,
  price_tax         REAL,
  price_type        TEXT,
  price_valid_from  TEXT,
  price_territory   TEXT,
  discount_group    TEXT,
  net_weight        REAL,
  pack_length       REAL,
  pack_width        REAL,
  pack_depth        REAL,
  pack_weight       REAL,
  customs_number    TEXT,
  country_of_origin TEXT,
  rohs              INTEGER,
  ce_marking        INTEGER,
  battery_contained INTEGER,
  product_to_stock  INTEGER,
  warranty_business TEXT,
  warranty_consumer TEXT,
  shelf_life        TEXT,
  reach_info        TEXT,
  reach_listdate    TEXT,
  valid_from        TEXT,
  UNIQUE (brand, supplier_pid)              -- article no is unique within a brand only
);

DROP TABLE IF EXISTS product_feature;
CREATE TABLE product_feature (
  product_id INTEGER NOT NULL,
  fname      TEXT NOT NULL,   -- ETIM feature code (EF000007)
  value_idx  INTEGER NOT NULL DEFAULT 0,
  fvalue     TEXT,            -- code (EV000154), number or true/false
  details    TEXT,
  funit      TEXT
);

DROP TABLE IF EXISTS product_keyword;
CREATE TABLE product_keyword (
  product_id INTEGER NOT NULL, lang TEXT, keyword TEXT
);

DROP TABLE IF EXISTS product_mime;
CREATE TABLE product_mime (
  product_id  INTEGER NOT NULL,
  code        TEXT,    -- MD22 = data sheet, MD52 = CE declaration, MD19 = photometry ...
  designation TEXT,
  filename    TEXT,
  source      TEXT,    -- URL
  lang        TEXT,
  ord         INTEGER,
  issue_date  TEXT
);

DROP TABLE IF EXISTS product_ref;
CREATE TABLE product_ref (
  product_id INTEGER NOT NULL,
  ref_type   TEXT,     -- accessories | similar
  prod_id_to TEXT,
  descr      TEXT
);

DROP TABLE IF EXISTS product_price;
CREATE TABLE product_price (
  product_id  INTEGER NOT NULL,
  price_type  TEXT, amount REAL, currency TEXT, tax REAL,
  lower_bound TEXT, territory TEXT, valid_from TEXT
);

DROP TABLE IF EXISTS packing_unit;
CREATE TABLE packing_unit (
  product_id INTEGER NOT NULL,
  code TEXT, qty_min TEXT, qty_max TEXT, parts TEXT, pkg_break TEXT,
  volume REAL, weight REAL, length REAL, width REAL, depth REAL, gtin TEXT
);

DROP TABLE IF EXISTS characteristic;
CREATE TABLE characteristic (
  product_id INTEGER NOT NULL,
  code TEXT, name TEXT, value_de TEXT, value_en TEXT
);

-- MIME code -> human readable label (derived from the data + EDXF 5.0)
DROP TABLE IF EXISTS mime_code;
CREATE TABLE mime_code (code TEXT PRIMARY KEY, label TEXT, kind TEXT);

-- Local PDF documents (certificates, catalogues, declarations)
DROP TABLE IF EXISTS doc;
CREATE TABLE doc (
  id        INTEGER PRIMARY KEY,
  brand     TEXT NOT NULL DEFAULT '',
  path      TEXT NOT NULL UNIQUE,   -- relative to the project root
  category  TEXT,                   -- directory name
  filename  TEXT,
  title     TEXT,
  bytes     INTEGER,
  pages     INTEGER,
  sha1      TEXT,
  indexed_at TEXT
);

-- Page by page text of the PDFs (for catalogue<->product linking and page numbers)
DROP TABLE IF EXISTS doc_page;
CREATE TABLE doc_page (
  doc_id  INTEGER NOT NULL,
  page_no INTEGER NOT NULL,
  text    TEXT
);

-- Document <-> product series link (produced by etl/link_catalogs.py)
DROP TABLE IF EXISTS doc_series;
CREATE TABLE doc_series (
  doc_id      INTEGER NOT NULL,
  brand       TEXT NOT NULL DEFAULT '',
  series      TEXT NOT NULL,   -- the RAW product.series_en value; joins go through this
  page_count  INTEGER,
  first_page  INTEGER,
  pages       TEXT,            -- summary like "1,6,12-14"
  in_filename INTEGER,
  strength    TEXT,            -- 'primary' | 'mentioned'
  score       REAL
);
