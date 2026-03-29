---
name: lookup-trade
description: >
  Search macmap.org trade data: tariffs, custom duties, trade remedies, and
  NTM (non-tariff measures) by reporter country, partner country, and product
  (HS code or keyword). Use when you need tariff rates, trade barriers, or
  regulatory measures between two countries for a product.
argument-hint: "[--reporter COUNTRY] [--partner COUNTRY] [--product PRODUCT] [--type taxes|duties|remedies|ntm|all]"
---

# Lookup Trade Data

Search pre-crawled macmap.org trade data (tariffs, duties, trade remedies, NTM measures) stored in Typesense.

## How to Run

```bash
python ${CLAUDE_SKILL_DIR}/scripts/search.py $ARGUMENTS
```

## Arguments

| Argument | Description | Example |
|----------|-------------|---------|
| `--reporter` | Reporter/importing country (name, ISO2, ISO3, or macmap code) | `US`, `USA`, `842`, `"United States"` |
| `--partner` | Partner/exporting country (name, ISO2, ISO3, or macmap code) | `IN`, `IND`, `356`, `India` |
| `--product` | HS code or product keyword | `26201930`, `"zinc dross"`, `beef` |
| `--type` | Data type to show: `all`, `duties`, `taxes`, `remedies`, `ntm` | `duties` |
| `--limit` | Max results (default: 10) | `20` |

## Examples

### Search by exact codes
```bash
python ${CLAUDE_SKILL_DIR}/scripts/search.py --reporter 842 --partner 004 --product 26201930
```

### Search by country names and product keyword
```bash
python ${CLAUDE_SKILL_DIR}/scripts/search.py --reporter US --partner India --product "zinc dross"
```

### Show only custom duties
```bash
python ${CLAUDE_SKILL_DIR}/scripts/search.py --reporter US --partner India --product 26201930 --type duties
```

### Show only trade remedies
```bash
python ${CLAUDE_SKILL_DIR}/scripts/search.py --reporter US --partner Afghanistan --product 26201930 --type remedies
```

### Show only NTM measures
```bash
python ${CLAUDE_SKILL_DIR}/scripts/search.py --reporter US --partner Afghanistan --product 26201930 --type ntm
```

### Search with product keyword (resolves via HS code lookup)
```bash
python ${CLAUDE_SKILL_DIR}/scripts/search.py --reporter US --partner Vietnam --product "steel pipes"
```

## Data Sources

Data is pre-crawled from macmap.org APIs:
- **Custom duties** — Applied tariff rates (MFN, preferential, general)
- **Taxes** — Internal taxes and charges on imports
- **Trade remedies** — Anti-dumping, countervailing duties, safeguard measures
- **NTM measures** — Non-tariff measures (labelling, packaging, licensing, etc.)

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TYPESENSE_API_KEY` | `xyz` | Typesense API key |
| `TYPESENSE_HOST` | `localhost` | Typesense server host |
| `TYPESENSE_PORT` | `8108` | Typesense server port |
| `TYPESENSE_PROTOCOL` | `http` | Protocol |

## Setup (one-time)

1. Index countries: `python scripts/fetch_macmap_countries.py`
2. Index HS codes: `python scripts/fetch_hscodes.py`
3. Crawl trade data: `python scripts/fetch_macmap_trade.py --reporters 842`
