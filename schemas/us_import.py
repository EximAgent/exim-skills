"""US Imports — Bill of Lading data from US Customs (CBP/AMS).

Foreign exporters → US importers. `destination_country` is always 'us'.
Each row is one container/shipment line.
"""

from __future__ import annotations

SOURCE = "us_import"

# Maps raw xlsx column names → normalized field names from schemas/shared.py
FIELD_MAP = {
    "System Identity Id": "source_id",
    "Actual Arrival Date": "record_date",
    "Shipper Name": "exporter_name",
    "Shipper Address": "exporter_address",
    "Country": "origin_country",
    "Consignee Name": "importer_name",
    "Consignee Address": "importer_address",
    "Notify Party Name": "notify_party_name",
    "Notify Party Address": "notify_party_address",
    "HS Code": "hs_code",
    "Product Desc": "product_description",
    "Weight in KG": "weight_gross_kg",
    "Quantity": "quantity",
    "Quantity Unit": "quantity_unit",
    "Container Id": "container_id",
    "Container Type": "container_type",
    "Container Size": "container_size",
    "TEU": "teu",
    "Mode of Transportation": "transport_mode",
    "Loading Port": "loading_port",
    "Unloading Port": "unloading_port",
    "Vessel Name": "vessel_name",
    "Vessel Code": "vessel_code",
    "Voyage": "voyage",
    "Bill of Lading": "bill_of_lading",
    "Master Bill of Lading": "master_bill_of_lading",
    "Carrier SASC Code": "carrier_code",
}

# Set fixed values for this source (always populated)
FIXED_VALUES = {
    "destination_country": "us",
}


def build_id(row: dict, normalized: dict) -> str:
    """Each US import row has a unique System Identity Id."""
    return f"{SOURCE}_{normalized.get('source_id', 'unknown')}"
