"""Shared schemas, models, and data cleaning helpers used by all source-specific schemas.

Country-specific field mappings live in their own files (us_import.py, us_export.py, etc.).
This file only contains things that work across ALL sources.
"""

from __future__ import annotations

import re
import pycountry
from pydantic import BaseModel


# =================== Country normalization ===================
# All country fields are stored as lowercase ISO 3166-1 alpha-2 (e.g. "cn", "us", "ve")

def _build_country_lookup() -> dict[str, str]:
    """Build lookup from pycountry: alpha-2, alpha-3, name, official_name, common_name."""
    lookup = {}
    for c in pycountry.countries:
        a2 = c.alpha_2.lower()
        lookup[c.alpha_2.upper()] = a2
        lookup[c.alpha_3.upper()] = a2
        lookup[c.name.upper()] = a2
        if hasattr(c, "official_name"):
            lookup[c.official_name.upper()] = a2
        if hasattr(c, "common_name"):
            lookup[c.common_name.upper()] = a2
    return lookup


_COUNTRY_LOOKUP = _build_country_lookup()

# Substring rules for ambiguous/Spanish names in trade data.
# Only add entries that pycountry can't handle.
_COUNTRY_CONTAINS: list[tuple[str, str]] = [
    ("KOREA", "kr"),       # trade data "KOREA" always means South Korea
    ("ESTADOS UNIDOS", "us"),
    ("KOSOVO", "xk"),      # not in ISO 3166
    ("PALESTINE", "ps"),
]


# =================== Pydantic models ===================

class SearchCandidate(BaseModel):
    """A single Google search result."""
    url: str
    full_url: str
    title: str
    snippet: str
    position: int


class Company(BaseModel):
    """A company extracted from trade data, enriched through the pipeline.

    This is the schema for each line in the pipeline JSONL file.
    Fields are added progressively as the pipeline runs.
    """

    # --- Required (from raw trade data) ---
    company_name: str           # current name (may be Gemini-split version after triage)
    raw_name: str | None = None  # original name from xlsx (preserved through splits)
    address: str | None = None
    country: str | None = None
    buyer: str | None = None
    product_desc: str | None = None
    hs_code: str | None = None

    # --- Phase 1: LLM triage (ask_llm) ---
    llm_url: str | None = None
    llm_confidence: int | None = None  # 0, 1, or 2
    llm_reason: str | None = None
    llm_cost: float | None = None
    llm_tokens: int | None = None

    # --- Phase 2: Google search ---
    search_query: str | None = None
    search_candidates: list[SearchCandidate] | None = None

    # --- Phase 3: Agent reasoning ---
    website: str | None = None

    def to_llm_context(self) -> str:
        """Build the LLM prompt context from available fields."""
        parts = [f"Company name: {self.company_name}"]
        if self.address:
            parts.append(f"Address: {self.address}")
        if self.country:
            parts.append(f"Country: {self.country}")
        if self.buyer:
            parts.append(f"Buyer/partner: {self.buyer}")
        if self.product_desc:
            parts.append(f"Products/trade: {self.product_desc}")
        if self.hs_code:
            parts.append(f"HS code: {self.hs_code}")
        return "\n".join(parts)


Company.model_rebuild()


class CompanyInfo(BaseModel):
    """Company profile extracted from website. Stored in Typesense `companyInfo`.

    id = domain (e.g. "swiftbeef.com")
    """

    id: str
    company_name: str
    website: str
    industry: str | None = None
    products: list[str] | None = None
    description: str | None = None
    headquarters: str | None = None
    country: str | None = None
    employees: str | None = None
    year_founded: int | None = None
    parent_company: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None


# =================== Data cleaners ===================

def clean_str(val) -> str | None:
    """Convert any xlsx cell value to clean string or None.

    Collapses internal whitespace so names match consistently across tables.
    """
    if val is None:
        return None
    s = re.sub(r'\s+', ' ', str(val)).strip()
    return s if s and s != "None" else None


def clean_float(val) -> float | None:
    """Convert xlsx cell to float or None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def clean_int(val) -> int | None:
    """Convert xlsx cell to int or None. Handles Excel date corruption."""
    if val is None:
        return None
    try:
        return int(float(str(val).replace("-", "").replace(" ", "")[:8]))
    except (ValueError, TypeError):
        return None


def clean_hs_code(val) -> str | None:
    """Convert HS code to canonical string form.

    Handles common xlsx and source-data quirks:
      - Float repr: 847439.0 → "847439"
      - Dot/space/hyphen separators: "8474.39.00", "8474 39 00", "8474-39-00" → "84743900"
      - Lost leading zeros from Excel: 301.0 (was 0301) → "0301"

    Rejects (returns None):
      - All zeros (meaningless): "0", "00", "000000" → None
      - Codes > 10 digits (anomaly): "84743900001234" → None
      - Non-digit content: "ex8474", "8474/8475", letters, negatives → None
      - Empty / whitespace / "None"

    HS codes are always even length (2/4/6/8/10 digits). Odd-length codes
    are padded with a leading zero (Excel strips leading zeros from numerics).
    """
    if val is None:
        return None

    # Numeric types: reject negatives and non-integer floats
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if val < 0:
            return None
        if isinstance(val, float):
            if not val.is_integer():
                return None
            val = int(val)
        s = str(val)
    else:
        s = str(val).strip()
        if not s or s == "None":
            return None
        # Strip trailing .0 from string-form floats
        if s.endswith(".0"):
            s = s[:-2]
        # Strip common separators inside the code (dot, space, hyphen)
        s = s.replace(".", "").replace(" ", "").replace("-", "")

    if not s.isdigit():
        return None
    # Reject all-zero codes (meaningless)
    if int(s) == 0:
        return None
    # HS codes have a max of 10 digits across all national systems
    if len(s) > 10:
        return None
    # Pad odd-length codes (Excel lost a leading zero)
    if len(s) % 2 == 1:
        s = "0" + s
    return s


def hs_code_6(full_code: str | None) -> str | None:
    """Return the first 6 digits (international subheading) of an HS code.

    If the source code is shorter than 6 digits (rare — usually only chapter/heading
    level codes), pad with trailing zeros to reach 6.
    """
    if not full_code:
        return None
    if len(full_code) >= 6:
        return full_code[:6]
    return full_code.ljust(6, "0")


# =================== Transport mode normalization ===================
# Normalize to: maritime, air, road, rail, river, mail, fixed_installation, multimodal, unknown

_TRANSPORT_MODE_MAP = {
    # Spanish (Colombian)
    "marítimo": "maritime",
    "aéreo": "air",
    "terrestre (carretero)": "road",
    "terrestre (ferroviario)": "rail",
    "fluvial": "river",
    "postal": "mail",
    "instalación fija": "fixed_installation",
    "multimodal": "multimodal",
    # English
    "maritime": "maritime",
    "ocean": "maritime",
    "sea": "maritime",
    "vessel": "maritime",
    "air": "air",
    "road": "road",
    "truck": "road",
    "rail": "rail",
    "train": "rail",
    "river": "river",
    "mail": "mail",
    # US Schedule D codes (Mode of Transportation)
    "10": "maritime",
    "11": "maritime",
    "12": "maritime",
    "20": "rail",
    "21": "rail",
    "30": "road",
    "31": "road",
    "32": "road",
    "33": "road",
    "40": "air",
    "41": "air",
    "50": "mail",
    "60": "fixed_installation",
    "70": "multimodal",
}


def clean_transport_mode(val) -> str | None:
    """Normalize transport mode to canonical English: maritime, air, road, rail, river, mail."""
    if val is None:
        return None
    s = str(val).strip().lower()
    if not s or s == "none":
        return None
    # Strip ".0" from numeric codes
    if s.endswith(".0"):
        s = s[:-2]
    return _TRANSPORT_MODE_MAP.get(s, s)


# =================== Quantity unit normalization ===================
# Normalize to UN/CEFACT codes where possible: KGM, LTR, MTR, PCE, etc.

_QUANTITY_UNIT_MAP = {
    # Spanish (Colombian)
    "kilogramo": "kg",
    "kilogramos": "kg",
    "kilo": "kg",
    "litro": "L",
    "litros": "L",
    "metro": "m",
    "metros": "m",
    "metro cuadrado": "m2",
    "metro cubico": "m3",
    "unidad": "unit",
    "unidades": "unit",
    "tonelada": "ton",
    "toneladas": "ton",
    "gramo": "g",
    "gramos": "g",
    "millar": "thousand",
    "par": "pair",
    "docena": "dozen",
    # English / US standard
    "kg": "kg",
    "kgs": "kg",
    "l": "L",
    "ltr": "L",
    "g": "g",
    "ton": "ton",
    "mt": "ton",  # metric ton
    "lbs": "lb",
    "lb": "lb",
    "ctn": "carton",
    "cartons": "carton",
    "ctns": "carton",
    "pkg": "package",
    "pkgs": "package",
    "pcs": "piece",
    "pc": "piece",
    "piece": "piece",
    "pieces": "piece",
    "bag": "bag",
    "bags": "bag",
    "bx": "box",
    "box": "box",
    "boxes": "box",
    "drum": "drum",
    "drums": "drum",
    "pallet": "pallet",
    "pallets": "pallet",
    "plt": "pallet",
    "bulk": "bulk",
    "unit": "unit",
    "units": "unit",
    "set": "set",
    "sets": "set",
    "roll": "roll",
    "rolls": "roll",
    "m": "m",
    "m2": "m2",
    "m3": "m3",
    "pair": "pair",
    "pairs": "pair",
    "dozen": "dozen",
}


def clean_quantity_unit(val) -> str | None:
    """Normalize quantity unit to canonical short form (kg, L, m, unit, carton, etc.)."""
    if val is None:
        return None
    s = str(val).strip().lower()
    if not s or s == "none":
        return None
    return _QUANTITY_UNIT_MAP.get(s, s)


def clean_country_code(val) -> str | None:
    """Normalize any country representation to lowercase ISO 3166-1 alpha-2.

    Resolution order:
    1. Direct lookup (alpha-2, alpha-3, name, official_name, common_name)
    2. Strip parenthetical, retry
    3. Comma split, retry first part
    4. Substring rules for trade-data-specific names
    5. pycountry fuzzy search as last resort
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s or s == "None":
        return None

    raw = s.upper()

    # 1. Direct lookup
    result = _COUNTRY_LOOKUP.get(raw)
    if result:
        return result

    # 2. Strip parenthetical
    result = _COUNTRY_LOOKUP.get(raw.split("(")[0].strip())
    if result:
        return result

    # 3. Comma split
    result = _COUNTRY_LOOKUP.get(raw.split(",")[0].strip())
    if result:
        return result

    # 4. Substring rules
    for substr, code in _COUNTRY_CONTAINS:
        if substr in raw:
            return code

    # 5. pycountry fuzzy search
    try:
        matches = pycountry.countries.search_fuzzy(s)
        if matches:
            return matches[0].alpha_2.lower()
    except LookupError:
        pass

    return None


# Type-specific cleaners keyed by normalized field name.
# Sources reference these by name in their FIELD_MAP.
FIELD_CLEANERS: dict[str, callable] = {
    "source_id": clean_str,
    "record_date": clean_int,
    "exporter_name": clean_str,
    "exporter_address": clean_str,
    "exporter_country": clean_country_code,
    "exporter_tax_id": clean_str,
    "importer_name": clean_str,
    "importer_address": clean_str,
    "importer_country": clean_country_code,
    "notify_party_name": clean_str,
    "notify_party_address": clean_str,
    "hs_code": clean_hs_code,
    "product_description": clean_str,
    "value_fob_usd": clean_float,
    "value_fob_local": clean_float,
    "freight_usd": clean_float,
    "insurance_usd": clean_float,
    "weight_gross_kg": clean_float,
    "weight_net_kg": clean_float,
    "quantity": clean_float,
    "quantity_unit": clean_quantity_unit,
    "container_id": clean_str,
    "container_type": clean_str,
    "container_size": clean_str,
    "container_quantity": clean_int,
    "teu": clean_int,
    "transport_mode": clean_transport_mode,
    "loading_port": clean_str,
    "loading_port_code_raw": clean_str,
    "unloading_port": clean_str,
    "unloading_port_code_raw": clean_str,
    "vessel_name": clean_str,
    "vessel_code": clean_str,
    "voyage": clean_str,
    "bill_of_lading": clean_str,
    "master_bill_of_lading": clean_str,
    "carrier_code": clean_str,
    "carrier_name": clean_str,
    "place_of_receipt": clean_str,
    "origin_country": clean_country_code,
    "destination_country": clean_country_code,
    "destination_country_name": clean_str,
    "origin_region": clean_str,
}
