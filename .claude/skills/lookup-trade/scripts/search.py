"""Search macmap.org trade data in Typesense.

Resolves country names/ISO codes to macmap codes and product keywords to HS codes,
then searches the macmap_trade collection.

Usage:
    python search.py --reporter US --partner India --product "zinc dross"
    python search.py --reporter 842 --partner 004 --product 26201930 --type duties
"""

import argparse
import asyncio
import json
import os
import sys

import typesense

# Add paths for shared modules
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_ROOT = os.path.dirname(SKILL_DIR)
sys.path.insert(0, os.path.join(SKILLS_ROOT, "lookup-hscode", "scripts"))

TRADE_COLLECTION = os.environ.get("TYPESENSE_MACMAP_TRADE_COLLECTION", "macmap_trade")
COUNTRIES_COLLECTION = os.environ.get("TYPESENSE_MACMAP_COUNTRIES_COLLECTION", "macmap_countries")


def get_typesense_client() -> typesense.Client:
    return typesense.Client({
        "api_key": os.environ.get("TYPESENSE_API_KEY", "xyz"),
        "nodes": [{
            "host": os.environ.get("TYPESENSE_HOST", "localhost"),
            "port": os.environ.get("TYPESENSE_PORT", "8108"),
            "protocol": os.environ.get("TYPESENSE_PROTOCOL", "http"),
        }],
        "connection_timeout_seconds": 10,
    })


def resolve_country(client: typesense.Client, query: str) -> tuple[str, str] | None:
    """Resolve a country name, ISO code, or macmap code to (code, name).

    Returns None if not found.
    """
    if not query:
        return None

    query = query.strip()

    # Try exact code match first
    try:
        doc = client.collections[COUNTRIES_COLLECTION].documents[query].retrieve()
        return doc["code"], doc["name"]
    except Exception:
        pass

    # Try ISO2/ISO3 filter
    for field in ["iso2", "iso3"]:
        try:
            result = client.collections[COUNTRIES_COLLECTION].documents.search({
                "q": "*",
                "filter_by": f"{field}:={query.upper()}",
                "per_page": 1,
            })
            if result["found"] > 0:
                doc = result["hits"][0]["document"]
                return doc["code"], doc["name"]
        except Exception:
            pass

    # Try text search on name
    try:
        result = client.collections[COUNTRIES_COLLECTION].documents.search({
            "q": query,
            "query_by": "name",
            "per_page": 1,
        })
        if result["found"] > 0:
            doc = result["hits"][0]["document"]
            return doc["code"], doc["name"]
    except Exception:
        pass

    return None


def resolve_product(client: typesense.Client, query: str) -> list[str]:
    """Resolve a product query to HS codes.

    If query looks like an HS code (digits only), returns it directly.
    Otherwise searches the hscodes collection for matching products.
    """
    query = query.strip()

    # If it's a numeric code, return directly
    if query.isdigit():
        return [query]

    # Search hscodes collection
    try:
        from lookup import search_hscodes
        hits = asyncio.run(search_hscodes(query, limit=5, mode="simple"))
        codes = [h["code"] for h in hits if len(h["code"]) >= 6]  # subheading level
        if codes:
            return codes
    except Exception:
        pass

    # Fallback: search hscodes via Typesense directly
    try:
        result = client.collections["hscodes"].documents.search({
            "q": query,
            "query_by": "name,full_path",
            "filter_by": "level:=subheading",
            "per_page": 5,
        })
        return [hit["document"]["code"] for hit in result["hits"]]
    except Exception:
        return []


def search_trade(
    client: typesense.Client,
    reporter_code: str | None = None,
    partner_code: str | None = None,
    product_codes: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search the macmap_trade collection with filters."""
    filters = []
    if reporter_code:
        filters.append(f"reporter_code:={reporter_code}")
    if partner_code:
        filters.append(f"partner_code:={partner_code}")
    if product_codes:
        if len(product_codes) == 1:
            filters.append(f"product_code:={product_codes[0]}")
        else:
            codes_str = ",".join(f"[{c}]" for c in product_codes)
            # Use multi-value filter for multiple codes
            code_filters = [f"product_code:={c}" for c in product_codes]
            filters.append(f"({' || '.join(code_filters)})")

    filter_by = " && ".join(filters) if filters else ""

    result = client.collections[TRADE_COLLECTION].documents.search({
        "q": "*",
        "filter_by": filter_by,
        "per_page": limit,
        "sort_by": "crawled_at:desc",
    })

    return [hit["document"] for hit in result["hits"]]


def format_output(docs: list[dict], show_type: str = "all") -> str:
    """Format trade data for display."""
    if not docs:
        return json.dumps({"status": "no_results", "message": "No trade data found for the given criteria"}, indent=2)

    results = []
    for doc in docs:
        entry = {
            "reporter": f"{doc['reporter_name']} ({doc['reporter_code']})",
            "partner": f"{doc['partner_name']} ({doc['partner_code']})",
            "product": f"{doc['product_code']}",
        }
        if doc.get("product_description"):
            entry["product_description"] = doc["product_description"]

        if show_type in ("all", "duties") and doc.get("custom_duties_json"):
            duties = json.loads(doc["custom_duties_json"])
            entry["custom_duties"] = {
                "applied_tariff": doc.get("applied_tariff", ""),
                "ave_tariff": doc.get("ave_tariff", ""),
                "tariff_regime": doc.get("tariff_regime", ""),
                "year": doc.get("year", ""),
                "details": duties.get("CustomDuty", []),
            }

        if show_type in ("all", "taxes") and doc.get("taxes_json"):
            taxes = json.loads(doc["taxes_json"])
            entry["taxes"] = taxes.get("TaxDataViewModels", [])

        if show_type in ("all", "remedies") and doc.get("trade_remedies_json"):
            remedies = json.loads(doc["trade_remedies_json"])
            entry["trade_remedies"] = {
                "types": doc.get("trade_remedy_types", []),
                "details": remedies.get("TradeRemedyData", []),
            }

        if show_type in ("all", "ntm") and doc.get("ntm_measures_json"):
            ntm = json.loads(doc["ntm_measures_json"])
            entry["ntm_measures"] = {
                "total_count": doc.get("ntm_measure_count", 0),
                "measure_titles": doc.get("ntm_measure_titles", []),
                "sections": ntm,
            }

        results.append(entry)

    return json.dumps({"status": "found", "count": len(results), "results": results}, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Search macmap.org trade data")
    parser.add_argument("--reporter", type=str, help="Reporter country (name, ISO2/3, or macmap code)")
    parser.add_argument("--partner", type=str, help="Partner country (name, ISO2/3, or macmap code)")
    parser.add_argument("--product", type=str, help="Product HS code or keyword")
    parser.add_argument("--type", type=str, default="all", choices=["all", "duties", "taxes", "remedies", "ntm"],
                        help="Type of data to show (default: all)")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")

    args = parser.parse_args()

    if not any([args.reporter, args.partner, args.product]):
        parser.print_help()
        sys.exit(1)

    client = get_typesense_client()

    # Resolve country codes
    reporter_code = None
    partner_code = None

    if args.reporter:
        resolved = resolve_country(client, args.reporter)
        if resolved:
            reporter_code, reporter_name = resolved
            print(f"[resolve] Reporter: {reporter_name} ({reporter_code})", file=sys.stderr)
        else:
            print(f"[error] Could not resolve reporter: {args.reporter}", file=sys.stderr)
            sys.exit(1)

    if args.partner:
        resolved = resolve_country(client, args.partner)
        if resolved:
            partner_code, partner_name = resolved
            print(f"[resolve] Partner: {partner_name} ({partner_code})", file=sys.stderr)
        else:
            print(f"[error] Could not resolve partner: {args.partner}", file=sys.stderr)
            sys.exit(1)

    # Resolve product
    product_codes = None
    if args.product:
        product_codes = resolve_product(client, args.product)
        if product_codes:
            print(f"[resolve] Product codes: {product_codes}", file=sys.stderr)
        else:
            print(f"[warn] Could not resolve product: {args.product}, searching text instead", file=sys.stderr)

    # Search
    docs = search_trade(client, reporter_code, partner_code, product_codes, args.limit)

    # If no results with product codes, try text search on product description
    if not docs and args.product and not args.product.isdigit():
        try:
            filters = []
            if reporter_code:
                filters.append(f"reporter_code:={reporter_code}")
            if partner_code:
                filters.append(f"partner_code:={partner_code}")
            filter_by = " && ".join(filters) if filters else ""

            result = client.collections[TRADE_COLLECTION].documents.search({
                "q": args.product,
                "query_by": "product_description,summary_text",
                "filter_by": filter_by,
                "per_page": args.limit,
            })
            docs = [hit["document"] for hit in result["hits"]]
        except Exception:
            pass

    print(format_output(docs, args.type))


if __name__ == "__main__":
    main()
