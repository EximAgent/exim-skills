"""US Exports — Outbound shipment data from US Customs.

US exporters → foreign importers. `origin_country` is always 'us'.
Note: US exports data has no field corresponding to the foreign importer name —
only US_Exporter is identified. Importer side is unknown from this dataset.
"""

from __future__ import annotations

SOURCE = "us_export"

FIELD_MAP = {
    "Act_Arrival_Date": "record_date",
    "HS_Code": "hs_code",
    "Product_Detailed_Description": "product_description",
    "US_Exporter": "exporter_name",
    "US_Exporter_Address": "exporter_address",
    "Port_Code_of_Departure": "loading_port_code_raw",
    "Port_of_Departure": "loading_port",
    "Country_of_Foreign_Port": "destination_country",
    "Bill_of_Lading_Nbr": "bill_of_lading",
    "Quantity": "quantity",
    "Quantity_Unit": "quantity_unit",
    "WEIGHT_IN_KG": "weight_gross_kg",
    "Container_Quantity": "container_quantity",
    "CONTAINER_ID": "container_id",
    "Carrier_Code": "carrier_code",
    "Carrier": "carrier_name",
    "Vessel_Name": "vessel_name",
    "Place_Receipt": "place_of_receipt",
    "Foreign_Port_Code": "unloading_port_code_raw",
    "Foreign_Port": "unloading_port",
    "Voyage": "voyage",
}

FIXED_VALUES = {
    "origin_country": "us",
    # US exports xlsx has no transport_mode column — infer from data:
    # vessel_name + container_id present → maritime (Bill of Lading shipping)
    "transport_mode": "maritime",
}


def build_id(row: dict, normalized: dict) -> str:
    """US exports has no source_id field — build one from BOL + HS code + container."""
    bol = normalized.get("bill_of_lading") or ""
    hs = normalized.get("hs_code") or ""
    container = normalized.get("container_id") or ""
    return f"{SOURCE}_{bol}_{hs}_{container}"
