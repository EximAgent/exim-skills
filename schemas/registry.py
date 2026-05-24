"""Source registry — collects all per-source schemas in one place.

To add a new country:
  1. Create schemas/{country}_{import|export}.py with FIELD_MAP, FIXED_VALUES, build_id()
  2. Import + register it below
"""

from __future__ import annotations

from schemas import us_import, us_export, co_export
from schemas.shared import FIELD_CLEANERS, clean_str, hs_code_6

# Registered sources
_SOURCES = {
    us_import.SOURCE: us_import,
    us_export.SOURCE: us_export,
    co_export.SOURCE: co_export,
}

# Backwards-compat alias used by extract_companies_from_xlsx and other consumers
SOURCE_FIELD_MAP = {name: mod.FIELD_MAP for name, mod in _SOURCES.items()}


def normalize_row(row_dict: dict, source: str) -> dict:
    """Convert a raw xlsx row dict to a normalized dict.

    Dispatches to the source-specific schema for field mapping and id generation.
    """
    if source not in _SOURCES:
        raise ValueError(f"Unknown source: {source}. Registered: {list(_SOURCES.keys())}")

    mod = _SOURCES[source]
    result = {"source": source}

    for src_col, dest_field in mod.FIELD_MAP.items():
        raw_val = row_dict.get(src_col)
        cleaner = FIELD_CLEANERS.get(dest_field, clean_str)
        result[dest_field] = cleaner(raw_val)

    # Apply fixed values for this source
    for k, v in mod.FIXED_VALUES.items():
        result[k] = v

    # Derive hs_code_6 (handles short codes via padding)
    result["hs_code_6"] = hs_code_6(result.get("hs_code"))

    # Build id via source-specific function
    result["id"] = mod.build_id(row_dict, result)

    # Ensure required fields
    result.setdefault("exporter_name", "")
    result.setdefault("importer_name", "")
    result.setdefault("record_date", 0)

    return result
