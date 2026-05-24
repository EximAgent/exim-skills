# EXIM Database Design

This doc describes the trade data + company info database design, the rationale
behind each decision, and the edge cases handled. The intended audience is engineers
building on top of this DB (especially the `companyInfo` worker).

---

## Overview

Two databases, each chosen for what it's good at:

| DB | Holds | Why |
|---|---|---|
| **ClickHouse** (`exim`) | `tradeData` (transactions) + `company_websites` (lookup) | Columnar, append-only, fast aggregations over millions of rows, native JSON + Array types |
| **Typesense** (TBD) | `companyInfo` (entity profiles) | Hybrid lexical + semantic search, designed for "find me companies that..." queries |

**Pipeline shape:**

```
raw xlsx (US/CO/...)
    │
    ▼  pipeline.py — 6 steps
extract raw names → dedup vs ClickHouse → Gemini triage →
Claude agent search (Serper + CF crawl) → save → ingest trade rows
    │
    ▼  ClickHouse
company_websites + tradeData  (built)
    │
    ▼  separate worker (TBD)
crawl each website → LLM extract structured profile → embed →
    │
    ▼  Typesense
companyInfo  (search/query layer)
```

---

## ClickHouse: `company_websites`

```sql
CREATE TABLE company_websites (
  source LowCardinality(String),   -- "us_import" | "us_export" | "co_export" | ...
  raw_name String,                 -- EXACTLY as it appears in tradeData (xlsx form, whitespace-normalized)
  name String,                     -- Gemini's parsed/cleaned company name (display only)
  website String,                  -- "https://example.com"
  found_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(found_at)
ORDER BY (source, raw_name, website)
```

### Design rationale

**Why `raw_name` as the join key (not the cleaned `name`)?**

`raw_name` is what's literally in the xlsx. After `clean_str` (whitespace collapse only) it's deterministic — same xlsx always produces the same key. This means:

1. Lookups from `tradeData` are direct: `WHERE raw_name = exporter_raw`.
2. Dedup check at pipeline Step 2 is cheap: "have we seen this raw name before?"
3. No LLM call needed for matching.

We considered using Gemini's cleaned `name` as the key, but rejected because:
- Non-deterministic — same input can produce slightly different output across runs.
- Re-running on the same raw name would create new "canonical" forms, breaking dedup.
- Joins would require Gemini-in-the-middle.

**Why multiple rows per `raw_name`?**

A single trade row's company name field sometimes contains **multiple distinct companies**, separated by `//`, `/`, `C/O`, etc. Examples seen in real data:

```
"JIANGSU JUMAO // X CARE MEDICAL"
"CABOT CORP\nC/O HARTLEY OIL"
"L & S SHRINK SYSTEMS INC. // OXYGEN DEVELOPMENT"
"ACUSHNET COMPANY / LINKS & KINGS - MORAINE COUNTRY CLUB"
```

These should produce **N rows** in `company_websites`, all sharing the same `raw_name` but with distinct (name, website) pairs:

```
("us_import", "CABOT CORP", "CABOT CORP",  "https://cabotcorp.com")
("us_import", "CABOT CORP", "HARTLEY OIL", "https://hartleyoil.com")
```

The `name` column stores the human-readable canonical name; the `website` column is the
true dedup key for "is this the same real company?" (see "Cross-source dedup" below).

**Why `ReplacingMergeTree(found_at)`?**

Inserts can be re-run idempotently. If a company is re-searched and we find a different website, the most recent `found_at` wins on merge. The `ORDER BY (source, raw_name, website)` defines uniqueness.

---

## ClickHouse: `tradeData`

```sql
CREATE TABLE tradeData (
  -- Identity
  id String,                                   -- "{source}_{source_id}[_line]"
  source LowCardinality(String),
  record_date UInt32,                          -- YYYYMMDD

  -- Parties (raw + arrays for multi-company splits)
  exporter_raw String,                         -- joins to company_websites.raw_name
  exporter_name Array(String),                 -- Gemini-cleaned names (1+ entries when raw has multiple companies)
  exporter_website Array(String),              -- websites paired BY INDEX with exporter_name
  importer_raw String,
  importer_name Array(String),
  importer_website Array(String),

  -- Geography (always set per source convention, ISO alpha-2 lowercase)
  origin_country LowCardinality(String),
  destination_country LowCardinality(String),

  -- Product
  hs_code String,                              -- as-reported (6 for US, 10 for CO)
  hs_code_6 String,                            -- first 6 digits — universal cross-country join key

  -- Quantity / weight (core analytics)
  weight_gross_kg Nullable(Float64),
  quantity Nullable(Float64),
  quantity_unit LowCardinality(String),        -- normalized: "kg", "L", "carton", "bulk", etc.

  -- Logistics (US-format, primary index target)
  transport_mode LowCardinality(String),       -- normalized: "maritime", "air", "road", "rail", "river"
  loading_port_code String,                    -- "58023"
  loading_port_name String,                    -- "PUSAN"
  unloading_port_code String,
  unloading_port_name String,
  vessel_name String,
  bill_of_lading String,
  carrier_code String,                         -- SCAC: "CMDU" (CMA CGM), "MAEU" (Maersk), etc.

  -- Source-specific fields → JSON blob
  extra String DEFAULT '{}'
)
ENGINE = ReplacingMergeTree()
ORDER BY (source, id)
PARTITION BY source
```

### Design rationale

**Why arrays for `exporter_name`/`exporter_website` (not single strings)?**

Same multi-company case as `company_websites`. One trade row could represent two distinct
entities sharing one shipment. Storing arrays preserves this:

```
exporter_raw:     "JIANGSU JUMAO // X CARE MEDICAL"
exporter_name:    ["JIANGSU JUMAO", "X CARE MEDICAL"]
exporter_website: ["https://jiangsujumao.com", "https://xcaremedical.com"]
```

Arrays are paired **by index** (position 0 of `name` matches position 0 of `website`).
Most rows have arrays of length 1 (normal single-company case).

**Why both `exporter_raw` and `exporter_name`?**

- `exporter_raw` = the join key to `company_websites.raw_name`. Always exactly as it appears in the xlsx.
- `exporter_name` = the cleaned/split name(s) for display and queries like "find rows for COMPANY X".

For 95% of rows they look identical. For multi-company splits they differ.

**Why `origin_country` / `destination_country` always set (not null)?**

Each source has a fixed direction. We bake this in at normalization time:

| Source | `origin_country` | `destination_country` |
|---|---|---|
| `us_import` | from data (e.g. `cn`, `kr`) | always `us` |
| `us_export` | always `us` | from data |
| `co_export` | always `co` | from data |

This avoids nulls on a heavily-queried field. The source dataset's "direction" defines the missing side.

**Why `extra` as JSON blob (not separate columns)?**

Each source has fields the others don't (US has containers, Colombia has FOB pricing, etc.).
Adding columns for every source's specific fields would balloon the schema. JSON keeps the
table lean — only fields that are queryable across sources are core columns.

ClickHouse JSON is queryable: `SELECT extra.exporter_tax_id FROM tradeData`. We use it for
~10-15 source-specific fields per row (see "Field availability matrix" below).

**Why `id = {source}_{source_id}[_line]`?**

- For US: `Sys Identity Id` is per-container unique → `us_import_6003202311210000001129`.
- For CO: `NUMERO_FORMULARIO` is per-declaration, but one declaration can have multiple
  line items (`NUMERO_SERIE`) — different products in the same shipment. We append the
  line number: `co_export_6007771529307_2.0`.
- For US Exports: no source_id field; we build `id` from BOL + HS + container as a stable
  composite: `us_export_NAM6444776_790200_TEMU0032791`.

`ReplacingMergeTree` upserts by `id` — re-ingesting the same xlsx is idempotent.

---

## Field availability matrix

What's actually populated by source:

| Field | us_import | us_export | co_export | Notes |
|---|---|---|---|---|
| `exporter_raw/name/website` | ✓ (foreign exporter) | ✓ (US co.) | ✓ (CO co.) | |
| `importer_raw/name/website` | ✓ (US importer) | ✗ | ✓ (foreign importer) | **CBP doesn't publish foreign importers in US export BOL data** |
| `origin_country` | from data | always `us` | always `co` | |
| `destination_country` | always `us` | from data | from data | |
| `hs_code` | 6-digit | 6-digit | 10-digit (Arancel) | US BOL is harmonized to 6-digit only |
| `hs_code_6` | always | always | always | Derived universal key |
| `weight_gross_kg` | ✓ | ✓ | ✓ | |
| `quantity` + `quantity_unit` | ✓ | ✓ | ✓ | All normalized |
| `transport_mode` | ✓ | inferred `maritime` | ✓ | US export xlsx has no field; inferred (BOL data is maritime) |
| `loading_port_code/name` | ✓ | ✓ | ✗ | DIAN doesn't track ports |
| `unloading_port_code/name` | ✓ | ✓ | ✗ | |
| `vessel_name` | ✓ | ✓ | ✗ | |
| `bill_of_lading` | ✓ | ✓ | ✗ | |
| `carrier_code` (SCAC) | ✓ | ✓ | ✗ | |
| `value_fob_usd` (in `extra`) | ✗ | ✗ | ✓ | **US BOL has no pricing — only customs entry data (CBP 7501) has it, which isn't published** |
| `exporter_tax_id` (NIT, in `extra`) | ✗ | ✗ | ✓ | Colombian NIT only |
| Container fields (in `extra`) | full | partial | ✗ | container_id, container_type, container_size, teu |
| `master_bill_of_lading` (in `extra`) | ✓ | ✗ | ✗ | |
| `notify_party_*` (in `extra`) | ✓ | ✗ | ✗ | |
| `freight_usd`, `insurance_usd` (in `extra`) | ✗ | ✗ | ✓ | DIAN cost breakdown |
| `weight_net_kg` (in `extra`) | ✗ | ✗ | ✓ | US only has gross |
| `origin_region` (in `extra`) | ✗ | ✗ | ✓ | Colombian internal region like "VALLE DEL CAUCA" |
| `product_description` (in `extra`) | ✓ | ✓ | ✗ | CO uses HS code only, no text description |

---

## Sample `extra` JSON content per source

### `us_import` extra
```json
{
  "exporter_address": "22 SECOND SECTION JIANSHE AVENUE...",
  "importer_address": "6425 POWERS FERRY RD N W STE 120 ATLANTA GA 30339 USA",
  "notify_party_name": "UNIT INTERNATIONAL INC",
  "notify_party_address": "9485 REGENCY SQUARE BLVD SUITE 110 JACKSONVILLE FL 32211",
  "product_description": "SHIPPER'S LOAD, COUNT & SEAL 1X40 RH (FCL)(CY CY) S.T.C IQF POLLOCK FILLETS",
  "container_id": "OERU4030553",
  "container_type": "45R1",
  "container_size": "4000*900*800",
  "teu": 2,
  "vessel_code": "9501344",
  "voyage": "MBY1E",
  "master_bill_of_lading": "COSU8027003720"
}
```

### `us_export` extra
```json
{
  "product_description": "SHREDDED ZINC SCRAP SAVES AS PER ISRIHS CODE: 7902",
  "exporter_address": "C/O BHATT INTERNATIONAL INC 5901 NW 63RD TER STE 175",
  "container_quantity": 1,
  "container_id": "TEMU0032791",
  "carrier_name": "COMPAGNIE MARITIME DAFFRETEMEN",
  "place_of_receipt": "DETROIT",
  "voyage": "0INF0E1MA"
}
```

### `co_export` extra
```json
{
  "exporter_address": "TV 5 A 45 91 OF 05",
  "importer_address": "CALLE SILVA CASA NO. 1-81 SECTOR CENTRO",
  "exporter_tax_id": "901570149",
  "destination_country_name": "Venezuela (República Bolivariana de)",
  "value_fob_usd": 17292.0,
  "value_fob_local": 65191358.76,
  "freight_usd": 6000.0,
  "insurance_usd": 180.0,
  "weight_net_kg": 27000.0,
  "origin_region": "VALLE DEL CAUCA"
}
```

---

## Normalization conventions

All normalizers live in `schemas/shared.py`. Centralized so behavior is consistent across sources.

### Country codes → ISO alpha-2 lowercase

`clean_country_code(val)` handles:

| Input | Output |
|---|---|
| `"CN"` (alpha-2) | `"cn"` |
| `"VEN"` (alpha-3) | `"ve"` |
| `"CN, CHINA"` (US format) | `"cn"` (comma split) |
| `"CHINA"` (full name) | `"cn"` |
| `"Venezuela (República Bolivariana de)"` (CO format) | `"ve"` (parenthetical stripped) |
| `"ESTADOS UNIDOS"` (Spanish) | `"us"` (substring rule) |
| `"REPUBLIC OF KOREA"` | `"kr"` (substring rule — pycountry would match North Korea otherwise) |
| `"VIETNAM"` (common name) | `"vn"` (pycountry fuzzy) |
| `"KOSOVO"` | `"xk"` (not in ISO 3166 — substring rule) |

Resolution order: direct pycountry lookup → strip parenthetical → comma split → substring rules → pycountry fuzzy search.

### Transport mode → canonical English

`clean_transport_mode(val)` maps Spanish + US codes + English to:

```
maritime | air | road | rail | river | mail | fixed_installation | multimodal
```

Examples: `"Marítimo"` → `"maritime"`, `"11.0"` (US Schedule D code) → `"maritime"`,
`"Terrestre (carretero)"` → `"road"`, `"40"` → `"air"`.

### Quantity unit → canonical short form

`clean_quantity_unit(val)` maps Spanish + English shorthand to:

```
kg, L, m, m2, m3, unit, ton, g, lb, carton, package, piece, bag, box, drum, pallet, bulk, pair, dozen, set, roll
```

Examples: `"Kilogramo"` → `"kg"`, `"Litro"` → `"L"`, `"CTN"` → `"carton"`, `"PKG"` → `"package"`.

### HS codes — see next section

---

## HS code design (important)

HS codes are the trickiest field in trade data. Here's the canonical approach.

### Two stored columns

| Column | Format | Example | Use |
|---|---|---|---|
| `hs_code` | as-reported, normalized | `847439`, `2202100000`, `0301` | Country-specific detail |
| `hs_code_6` | first 6 digits, padded if shorter | `847439`, `220210`, `030100` | **Universal cross-country join key** |

### Why no `hs_code_10`

We considered storing `hs_code_10` (US HTS-format 10-digit code), but rejected:

- **US BOL data is 6-digit**, not 10-digit. Both Imports and Exports xlsx come at the international subheading level. So `hs_code_10` would be empty for ALL our US data.
- **Colombian codes are 10-digit but NOT US HTS** — digits 7-10 of the Arancel mean different things than digits 7-10 of the HTS. Storing them in the same column would mislead joins.
- **8-digit sources** (EU CN codes) would need padding with `"00"` → fake digits.
- **For US tariff lookups on any row**, query the separate HTS lookup table by `hs_code_6` and get all matching 10-digit codes (typically 1-10 sub-classifications per 6-digit subheading). Picking the "right" 10-digit needs product context.

If/when we ingest US Customs entry data (CBP Form 7501) which uses real 10-digit HTS, add the column then. For BOL data, 2 columns is right.

### `clean_hs_code(val)` — edge cases handled

| Input | Output | Reason |
|---|---|---|
| `847439.0` | `"847439"` | Excel float repr |
| `"847439.0"` | `"847439"` | String form of float |
| `"8474.39.00"` | `"84743900"` | HTS dot format |
| `"8474-39-00"` | `"84743900"` | Hyphen format |
| `"8474 39 00"` | `"84743900"` | Space format |
| `301.0` | `"0301"` | Lost leading zero (cheese was 0301, Excel dropped it) → padded to even length |
| `"0301"` | `"0301"` | Already correct |
| `"2202100000"` | `"2202100000"` | Full 10-digit |
| `None` / `""` / `"   "` / `"None"` | `None` | Empty input |
| `"not-a-code"` | `None` | Non-digit |
| `847439.5` | `None` | Non-integer float (real decimal, not `.0`) |
| `-301` | `None` | Negative |
| `"0"` / `"0000"` / `"0000000000"` | `None` | All zeros (meaningless) |
| `"84743900001234"` | `None` | Too long (>10 digits, max HS length globally) |
| `"ex8474390000"` | `None` | EU "ex" tag — not supported, agent should pre-handle |
| `"8474/8475"` | `None` | Multi-code — agent should split before normalize |
| `True` / `False` | `None` | Booleans rejected explicitly |

### `hs_code_6(full_code)` — derivation

```python
def hs_code_6(full_code: str | None) -> str | None:
    if not full_code:
        return None
    if len(full_code) >= 6:
        return full_code[:6]
    return full_code.ljust(6, "0")  # 4-digit heading → 6-digit subheading via trailing zeros
```

Always exactly 6 characters when set.

---

## Cross-source company dedup (important design choice)

Different raw names for the same real company arise constantly:

```
us_import: "AVIANCA INC"             → avianca.com
co_export: "AVIANCA S.A"             → avianca.com
co_export: "AEROVIAS DEL CONTINENTE AMERICANO AVIANCA" → avianca.com
```

These should resolve to **ONE companyInfo document** keyed by domain.

**Two-phase dedup model:**

1. **`company_websites` is per-raw-name** (no dedup across sources). Three rows above.
2. **`companyInfo` is per-domain** (the website IS the canonical company identifier). One row.

Aggregation query for companyInfo build:
```sql
SELECT DISTINCT website
FROM company_websites
WHERE website != ''
```

Then for each unique website, the `companyInfo` worker crawls + extracts + writes one document.

**Why the website is the ground truth:**

- Normalization (`clean_str`, dropping legal suffixes) is heuristic and fuzzy.
- Same company has many name variants we can't catch.
- But same company has **one canonical domain**.
- Two raw names → same domain → same company. No fuzzy matching needed.

---

## Multi-company splits via Gemini (important design choice)

Trade data company fields sometimes pack multiple distinct companies into one string:

```
"L & S SHRINK SYSTEMS INC. // OXYGEN DEVELOPMENT"
"COMMERCIAL ZELECTA TRADING GROUP CORP - ESSENTIAL.S FLOWERS"
"GREEN GLOBAL CONNECTION INC // BDC CHICAGO DBA EVERFLOR"
"CABOT CORP\nC/O HARTLEY OIL RT 68 SOUTH..."
```

**Without splitting:** the pipeline searches for `"L & S SHRINK SYSTEMS INC. // OXYGEN DEVELOPMENT"` as one company and finds neither cleanly. The website search returns confused results.

**Our design: Gemini splits at the LLM triage step.** During Phase 3 (LLM triage), the prompt
asks Gemini to detect multi-company patterns and return a JSON list:

```json
[
  {"name": "L & S SHRINK SYSTEMS INC.", "url": null, "confidence": 0, "reason": "..."},
  {"name": "OXYGEN DEVELOPMENT", "url": "https://www.oxygendevelopment.com", "confidence": 2, "reason": "..."}
]
```

Each split entry then proceeds independently through Step 4 (agent search) and ends up as a
separate row in `company_websites` (all sharing the same `raw_name`).

**Important Gemini behavior:**

- **`//`, `/`, `C/O`** → these are usually true multi-company separators → split
- **`DBA`** ("doing business as") → SAME company with trade name → keep as one entry
- **Hyphenated** like `"COMMERCIAL ZELECTA TRADING GROUP CORP - ESSENTIAL.S FLOWERS"` → context-dependent (sometimes parent-division, sometimes truly two companies); Gemini decides.

**Why use Gemini for this and not regex?**

We tested regex-based splitting on `//` / `C/O`. It works ~80% of the time but breaks on:
- Punctuation inside company names: `"R&G S.A."` not `"R&G S.A. // [next company]"`
- Address fragments concatenated to names
- Inconsistent capitalization / abbreviation
- Edge cases like `"DBA"` which look like separators but aren't

Gemini at confidence 2 on the split decision is much more reliable. And we already pay for it
for the website knowledge check — splitting is essentially free additional output.

**The `raw_name` always preserves the original form** — only `name` and `website` reflect the split.

---

## Cross-source examples

Same company appearing in multiple sources gets unified at the `website` level:

```
company_websites:
  ("us_import", "AVIANCA INC",             "AVIANCA",  "https://avianca.com")
  ("co_export", "AVIANCA S.A",             "AVIANCA",  "https://avianca.com")
  ("co_export", "AEROVIAS DEL CONTINENTE AMERICANO AVIANCA",  "AVIANCA",  "https://avianca.com")

tradeData:
  Row A (us_import): exporter_raw="JBS USA",          importer_raw="AVIANCA INC"
    → exporter_website=["jbsusa.com"], importer_website=["avianca.com"]
  Row B (co_export): exporter_raw="WORLD KINECT COLOMBIA",  importer_raw="AVIANCA S.A"
    → exporter_website=["worldkinect.com"], importer_website=["avianca.com"]

companyInfo (TBD):
  id="avianca.com"  → ONE row, populated by separate worker after crawling the site
```

---

## ClickHouse `company_websites` lookup API

In `tools/db.py`:

```python
lookup_websites(client, source, raw_names) -> dict[str, list[tuple[str, str]]]
# Returns: {raw_name: [(name, website), ...]}
# Multiple (name, website) per raw_name if Gemini split it into multiple companies.

insert_websites(client, source, rows: list[tuple[str, str, str]])
# rows = [(raw_name, name, website), ...]
```

---

## Typesense: `companyInfo` (TBD — not yet built)

The `companyInfo` worker is a separate service. It reads from `company_websites` (unique
domains) and produces structured profiles for Typesense.

### Proposed schema

```python
class CompanyInfo(BaseModel):
    # Identity
    id: str                              # = domain, e.g. "swiftbeef.com"
    domain: str
    company_name: str
    legal_name: str | None = None

    # Crawl metadata
    crawl_level: Literal[0, 1, 2] = 0    # 0=stub, 1=homepage parsed, 2=multi-page complete
    crawled_at: int = 0                  # unix timestamp
    pages_crawled: int = 0
    pages: dict[str, str] = {}           # {"/": "...md...", "/about": "...md...", ...}

    # About
    description: str | None = None       # 1-3 sentence summary
    long_description: str | None = None  # full about text
    year_founded: int | None = None
    employees_range: str | None = None   # "10-50", "100-500", etc.

    # Location
    country: str | None = None           # ISO alpha-2
    hq_city: str | None = None
    hq_address: str | None = None
    offices: list[dict] = []             # [{"city": "...", "address": "...", "country": "us"}, ...]

    # Business
    industries: list[str] = []
    products: list[str] = []             # what they advertise (scraped from website)
    services: list[str] = []
    hs_codes_advertised: list[str] = []  # 6-digit codes inferred from products on website
    hs_codes_traded: list[str] = []      # 6-digit codes derived from tradeData
    target_markets: list[str] = []       # countries served
    certifications: list[str] = []       # ["USDA", "ISO 9001", "HACCP", "FDA"]

    # Trade signals
    is_exporter: bool | None = None
    is_importer: bool | None = None
    parent_company: str | None = None
    subsidiaries: list[str] = []

    # Contact
    emails: list[str] = []
    phones: list[str] = []
    social: dict[str, str] = {}          # {"linkedin": "...", "twitter": "...", "facebook": "..."}

    # Embeddings (for Typesense hybrid search)
    description_embedding: list[float] | None = None   # 768-dim Gemini text-embedding-004
```

### Crawl level progression

| Level | What's filled | Trigger |
|---|---|---|
| 0 | id, domain, company_name only | Just inserted, stub from `company_websites` |
| 1 | + description, country, products, industries (rough) | Homepage crawled, LLM extracted basics |
| 2 | + emails, contact, certifications, offices, full products | Multi-page crawl complete |

### `hs_codes_traded` vs `hs_codes_advertised`

Both 6-digit, but different sources:

- `hs_codes_traded` — **derived from ClickHouse**: what they've actually shipped per tradeData.
  ```sql
  SELECT DISTINCT hs_code_6
  FROM tradeData
  WHERE has(exporter_website, 'https://swiftbeef.com')
     OR has(importer_website, 'https://swiftbeef.com')
  ```
- `hs_codes_advertised` — **extracted by LLM from website**: what they say they sell.

These can diverge: a company's website mentions 3 product categories but their actual trade
shows shipments of 8. Useful for "they advertise organic beef but also ship dairy" insights.

### Storage decisions for the agent

1. **Raw markdown** (`pages` field) — store in same collection but mark as `index: false`
   in Typesense so it's stored as a blob but not searched. Allows re-extraction without
   re-crawling. If documents get too large (>100KB), split into `companyPages` collection.

2. **Embedding** — Gemini `text-embedding-004` produces 768-dim vectors. Use Typesense's
   hybrid search (lexical + vector) for "find me companies that..." queries.

3. **Crawl strategy** — agent-controlled per-page (not CF auto-follow). Why: agent picks
   which pages matter (about, contact, products), CF auto-follow picks links arbitrarily
   based on what it finds first.

### Build flow

```
For each unique domain in company_websites:
  1. Check Typesense: companyInfo[id=domain] exists with crawl_level=2? → skip
  2. CF /crawl homepage (limit=1) → pages["/"] = markdown, set crawl_level=1
  3. Spawn Claude agent (Haiku) with tools:
       - crawl_url(url): CF crawl any URL → returns markdown
       - finalize(profile): write final profile and exit
     Agent loop:
       a. Read homepage markdown, fill what fields it can
       b. Identify missing important fields (industry, products, contact, etc.)
       c. Find relevant links in markdown (about, contact, products, services)
       d. crawl_url(those links) → store in pages dict
       e. Re-extract fields from new content
       f. Repeat until enough info or max 5 pages
       g. Call finalize(profile)
  4. Embed description via Gemini text-embedding-004
  5. Upsert to Typesense companyInfo
```

### Reusable utilities for the worker

- `tools.crawl_page.crawl_url(url)` — CF crawl returning `{url, title, markdown, status}`
- `llm.get_embedding("gemini-embedding-004", text=...)` — 768-dim vector
- `tools.db.get_client()` — ClickHouse connection (for `hs_codes_traded` derivation)
- Claude Agent SDK pattern from `pipeline.py:step4_agent_search` — for the crawl-and-extract agent

---

## Files in this codebase

```
exim-skills/
├── pipeline.py                          # 6-step pipeline (xlsx → ClickHouse)
├── llm.py + models.yaml                 # LLM abstraction (Gemini, Anthropic, OpenAI)
├── schemas/
│   ├── shared.py                        # Pydantic models, all clean_* helpers, normalizers
│   ├── registry.py                      # SOURCE_FIELD_MAP + normalize_row() dispatcher
│   ├── us_import.py                     # US Imports BOL field map (Imports.xlsx)
│   ├── us_export.py                     # US Exports BOL field map (Exports.xlsx)
│   └── co_export.py                     # Colombian DIAN exports field map
├── tools/
│   ├── search.py                        # Serper Google search (filters directories)
│   ├── crawl_page.py                    # Cloudflare /crawl wrapper (returns markdown)
│   └── db.py                            # ClickHouse client + table mgmt + lookup/insert helpers
└── docs/
    └── DATABASE.md                      # This file
```

---

## Known data limitations

- **US BOL has no foreign importer for exports** — CBP doesn't publish foreign consignee on US export BOL data. `importer_raw` will always be empty for `us_export` rows. Workaround: commercial providers like Panjiva supplement this; or join against the destination country's import data.
- **US BOL has no FOB pricing** — neither imports nor exports. Cargo value is on the customs entry (CBP Form 7501) which is confidential. `value_fob_usd` is empty for all US rows. Only Colombia (and a few other countries publishing customs declarations) include FOB.
- **Captcha sites can't be crawled** — DataDome/Cloudflare-protected sites return 403 to both agent-browser AND CF crawl. Examples: nuuly.com, danisaflowers.com. Pipeline records what it can but skips these.
- **Excel mangling** — pre-handled in cleaners but worth knowing:
  - HS codes lose leading zeros (`0301` → `301.0`)
  - Dates can leak into TEU column (`2026-05-01 00:00:00` in TEU field)
  - Names get double spaces from cell formatting

---

## Cost & throughput (validated)

Per 100 unique companies through the pipeline (Haiku via Claude Code subscription):

| Component | Cost |
|---|---|
| Gemini triage (Phase 1) | $0.13 |
| Serper Google searches | ~$0.19 |
| Claude Haiku agents (Phase 2) | $0 (on subscription) or $0.82 (API pricing) |
| CF crawl during agent verify | ~$0.001 |
| **Total per 100 companies** | **~$0.32** (subscription) or **~$1.14** (API) |

At Cloudflare paid plan: 10 browser hours/month = ~26k crawls included for $5.
At Gemini free tier: under 500 req/day. For higher volume, paid Gemini.
