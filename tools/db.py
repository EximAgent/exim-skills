"""ClickHouse database client and table management.

Tables:
  company_websites — lookup: (source, name) → website
  tradeData — raw trade transactions
"""

from __future__ import annotations

import os

import clickhouse_connect


def get_client():
    """Create ClickHouse client from env vars."""
    return clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_PORT", "8443")),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DATABASE", "exim"),
        secure=os.environ.get("CLICKHOUSE_SECURE", "true").lower() == "true",
    )


def create_database():
    """Create database if it doesn't exist."""
    db_name = os.environ.get("CLICKHOUSE_DATABASE", "exim")
    client = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_PORT", "8443")),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        secure=os.environ.get("CLICKHOUSE_SECURE", "true").lower() == "true",
    )
    client.command(f"CREATE DATABASE IF NOT EXISTS {db_name}")
    client.close()
    print(f"✓ Database '{db_name}' ready")


def create_tables(client=None):
    """Create all tables if they don't exist."""
    if client is None:
        client = get_client()

    client.command("""
        CREATE TABLE IF NOT EXISTS company_websites (
            source LowCardinality(String),
            raw_name String,           -- exactly as in trade data (xlsx)
            name String,               -- Gemini's parsed/split name
            website String,
            found_at DateTime DEFAULT now()
        )
        ENGINE = ReplacingMergeTree(found_at)
        ORDER BY (source, raw_name, website)
    """)

    client.command("""
        CREATE TABLE IF NOT EXISTS tradeData (
            -- Identity
            id String,
            source LowCardinality(String),
            record_date UInt32,

            -- Parties (raw + arrays for multi-company splits)
            exporter_raw String DEFAULT '',
            exporter_name Array(String) DEFAULT [],
            exporter_website Array(String) DEFAULT [],
            importer_raw String DEFAULT '',
            importer_name Array(String) DEFAULT [],
            importer_website Array(String) DEFAULT [],

            -- Geography (always set based on source)
            origin_country LowCardinality(String) DEFAULT '',
            destination_country LowCardinality(String) DEFAULT '',

            -- Product (core)
            hs_code String DEFAULT '',
            hs_code_6 String DEFAULT '',

            -- Quantity (core analytics)
            weight_gross_kg Nullable(Float64),
            quantity Nullable(Float64),
            quantity_unit LowCardinality(String) DEFAULT '',

            -- Logistics (core)
            transport_mode LowCardinality(String) DEFAULT '',
            loading_port_code String DEFAULT '',
            loading_port_name String DEFAULT '',
            unloading_port_code String DEFAULT '',
            unloading_port_name String DEFAULT '',
            vessel_name String DEFAULT '',
            bill_of_lading String DEFAULT '',
            carrier_code LowCardinality(String) DEFAULT '',

            -- Everything else (source-specific) goes here as JSON
            -- Examples: container_id, container_type, teu, master_bill_of_lading,
            --   notify_party_name, freight_usd, insurance_usd, voyage,
            --   exporter_tax_id, weight_net_kg, value_fob_local, etc.
            extra String DEFAULT '{}'
        )
        ENGINE = ReplacingMergeTree()
        ORDER BY (source, id)
        PARTITION BY source
    """)

    print("✓ Tables created")


def lookup_websites(client, source: str, raw_names: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Lookup existing (name, website) pairs for a list of raw names.

    Returns: {raw_name: [(name1, website1), (name2, website2), ...]}
    Multi-company splits result in multiple entries per raw_name.
    """
    if not raw_names:
        return {}

    result = client.query(
        "SELECT raw_name, name, website FROM company_websites "
        "WHERE source = {source:String} AND raw_name IN {names:Array(String)}",
        parameters={"source": source, "names": raw_names},
    )
    out: dict[str, list[tuple[str, str]]] = {}
    for row in result.result_rows:
        out.setdefault(row[0], []).append((row[1], row[2]))
    return out


def insert_websites(client, source: str, rows: list[tuple[str, str, str]]):
    """Insert (raw_name, name, website) rows into company_websites table."""
    if not rows:
        return

    client.insert(
        "company_websites",
        [[source, raw_name, name, website] for raw_name, name, website in rows],
        column_names=["source", "raw_name", "name", "website"],
    )
    print(f"  Inserted {len(rows)} websites into company_websites")


def insert_trade_records(client, records: list[dict]):
    """Insert normalized trade records into tradeData table."""
    if not records:
        return

    columns = [
        "id", "source", "record_date",
        "exporter_raw", "exporter_name", "exporter_website",
        "importer_raw", "importer_name", "importer_website",
        "origin_country", "destination_country",
        "hs_code", "hs_code_6",
        "weight_gross_kg", "quantity", "quantity_unit",
        "transport_mode",
        "loading_port_code", "loading_port_name",
        "unloading_port_code", "unloading_port_name",
        "vessel_name", "bill_of_lading", "carrier_code",
        "extra",
    ]

    rows = []
    for r in records:
        rows.append([r.get(c, "" if isinstance(r.get(c), str) else r.get(c)) for c in columns])

    client.insert("tradeData", rows, column_names=columns)
    print(f"  Inserted {len(rows)} trade records")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=".env")
    client = get_client()
    create_tables(client)
    print(f"Database: {os.environ.get('CLICKHOUSE_DATABASE', 'exim')}")
    print(f"company_websites rows: {client.command('SELECT count() FROM company_websites')}")
    print(f"tradeData rows: {client.command('SELECT count() FROM tradeData')}")
