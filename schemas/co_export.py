"""Colombian Exports — DIAN customs declarations.

Colombian exporters → foreign importers. `origin_country` is always 'co'.
One declaration (NUMERO_FORMULARIO) can have multiple line items (NUMERO_SERIE).
"""

from __future__ import annotations

from schemas.shared import clean_str

SOURCE = "co_export"

FIELD_MAP = {
    "NUMERO_FORMULARIO": "source_id",
    "FECHA_DECLARACION_EXPORTACION": "record_date",
    "RAZON_SOCIAL_EXPORTADOR": "exporter_name",
    "DIREC_EXPORTADOR": "exporter_address",
    "NIT_EXPORTADOR": "exporter_tax_id",
    "RAZON_SOCIAL_DESTINATARIO": "importer_name",
    "DOMICILIO_DESTINATARIO": "importer_address",
    "COD_PAIS_DESTINO": "destination_country",
    "PAIS_DESTINO_FINAL": "destination_country_name",
    "SUBPARTIDA": "hs_code",
    "VALOR_FOB_USD": "value_fob_usd",
    "VALOR_FOB_PESOS": "value_fob_local",
    "VALOR_SERIE_FLETES_USD": "freight_usd",
    "VALOR_SERIE_SEGUROS_USD": "insurance_usd",
    "PESO_BRUTO_KGS": "weight_gross_kg",
    "PESO_NETO_KGS": "weight_net_kg",
    "CANTIDAD_UNIDADES_FISICAS": "quantity",
    "UNIDAD_FISICA": "quantity_unit",
    "MODO_TRANSPORTE": "transport_mode",
    "REGION_PROCEDENCIA": "origin_region",
    "REGION_DE_ORIGEN": "origin_region",
}

FIXED_VALUES = {
    "origin_country": "co",
}


def build_id(row: dict, normalized: dict) -> str:
    """Combine NUMERO_FORMULARIO + NUMERO_SERIE for unique line items."""
    source_id = normalized.get("source_id") or "unknown"
    line_no = clean_str(row.get("NUMERO_SERIE"))
    if line_no:
        return f"{SOURCE}_{source_id}_{line_no}"
    return f"{SOURCE}_{source_id}"
