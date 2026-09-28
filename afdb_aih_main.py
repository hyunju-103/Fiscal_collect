from __future__ import annotations

import os
import re
import sys
import time
import shutil
import webbrowser
import json
import socket
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import urllib3

from common import standardize, validate_observations, run_stamp

SOURCE = "AFDB"
COLLECTOR_VERSION = "AFDB_FINAL_V6_PER_INDICATOR_SDMX_20260920"

DATASET_ID = "nbyenxf"
HOST = "https://dataportal.opendataforafrica.org"
DATASET_PAGE = (
    "https://dataportal.opendataforafrica.org/nbyenxf/"
    "afdb-socio-economic-database-1960-2024"
)

# =====================================================================
# 1. APPROVED LOCKED MAPPINGS
# =====================================================================
EXPECTED_LOCKED = {
    "1000390": {"wbfd_mnemonic": "GGREVDRCTCN", "indicator_original": "Direct taxes"},
    "1000310": {"wbfd_mnemonic": "GGEXPCRNTCN", "indicator_original": "Current expenditure"},
    "1000380": {"wbfd_mnemonic": "GGEXPWAGECN", "indicator_original": "Wages and salaries"},
    "1005810": {"wbfd_mnemonic": "GGEXPINTPCN", "indicator_original": "Interest payments"},
    "1005800": {"wbfd_mnemonic": "GGEXPTOTRCN", "indicator_original": "Other current expenditure"},
    "1000300": {"wbfd_mnemonic": "GGEXPCAPTCN", "indicator_original": "Capital expenditure"},
    "1001860": {"wbfd_mnemonic": "GGDBTEXTLCN", "indicator_original": "Total External Public Debt"},
    "1001900": {"wbfd_mnemonic": "GGDBTDOMLCN", "indicator_original": "Total Government Domestic debt"},
    "1002130": {"wbfd_mnemonic": "GGDBTINTCN", "indicator_original": "Total debt interest paid"},
}
EXPECTED_CODES = set(EXPECTED_LOCKED)

# =====================================================================
# 2. COUNTRY MEMBER MAPPING
#    Recovered from the SDMX + JSON links exposed by the AfDB Data Finder UI.
# =====================================================================
_ALL_COUNTRY_MEMBER_KEYS = (
    "1000000+1000010+1000020+1000030+1000040+1000050+1000060+1000070+"
    "1000080+1000090+1000100+1000110+1000120+1000130+1000140+1000150+"
    "1000160+1000170+1000180+1000190+1000200+1000210+1000220+1000230+"
    "1000240+1000250+1000260+1000270+1000280+1000290+1000300+1000310+"
    "1000320+1000330+1000340+1000350+1000360+1000370+1000380+1000390+"
    "1000400+1000410+1000420+1000430+1000440+1000450+1000460+1000470+"
    "1000480+1000490+1000500+1000510+1000520+1000530+1000540+1000550+"
    "1000560+1000570+1000580+1000590+1000610+1000620+1000630+1000640+"
    "1000650+1000660+1000670+1000680+1000690+1000700+1000710+1000720+"
    "1000730+1000740+1000750+1000760+1000770+1000780+1000790+1000800+"
    "1000810+1000820+1000830+1000880"
).split("+")

_ALL_COUNTRY_API_CODES = (
    "AFR,CENTRAL,CMR,CAF,TCD,ZAR,COG,GNQ,GAB,STP,EAST,BDI,COM,DJI,ERI,ETH,"
    "KEN,RWA,SYC,SOM,SSD,SDN,TZA,UGA,NORTH,DZA,EGY,LBY,MRT,MAR,TUN,SOUTH,"
    "AGO,BWA,LSO,MDG,MWI,MUS,MOZ,NAM,ZAF,SWZ,ZMB,ZWE,WEST,BEN,BFA,CPV,CIV,"
    "GMB,GHA,GIN,GNB,LBR,MLI,NER,NGA,SEN,SLE,TGO,AMU,CAEMC,COMESA,ECCAS,"
    "EAC,ECOWAS,FRZONE,SADC,WAEMU,LLC,OILEXP,OILIMP,OILPRD,SSA,ADFOnly,Blend,"
    "ADF,ADB,FRAGILE,ADF-Onl,ADF eli,FragSta,ADFRsRi,Pay.Gra"
).split(",")

if len(_ALL_COUNTRY_MEMBER_KEYS) != len(_ALL_COUNTRY_API_CODES):
    raise RuntimeError("AfDB country member mapping length mismatch.")

_INTERNAL_TO_API_COUNTRY = dict(zip(_ALL_COUNTRY_MEMBER_KEYS, _ALL_COUNTRY_API_CODES))

SOVEREIGN_AFRICA_API_CODES = {
    "DZA","AGO","BEN","BWA","BFA","BDI","CPV","CMR","CAF","TCD","COM","COG",
    "ZAR","CIV","DJI","EGY","GNQ","ERI","SWZ","ETH","GAB","GMB","GHA","GIN",
    "GNB","KEN","LSO","LBR","LBY","MDG","MWI","MLI","MRT","MUS","MAR","MOZ",
    "NAM","NER","NGA","RWA","STP","SEN","SYC","SLE","SOM","ZAF","SSD","SDN",
    "TZA","TGO","TUN","UGA","ZMB","ZWE",
}

COUNTRY_CODE_NORMALIZATION = {"ZAR": "COD"}

COUNTRY_MEMBER_MAP = {
    internal: COUNTRY_CODE_NORMALIZATION.get(api_code, api_code)
    for internal, api_code in _INTERNAL_TO_API_COUNTRY.items()
    if api_code in SOVEREIGN_AFRICA_API_CODES
}
COUNTRY_MEMBER_KEYS = list(COUNTRY_MEMBER_MAP.keys())

# =====================================================================
# 3. HTTP SESSION FOR SDMX DIRECT PATH
# =====================================================================
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "WorldBank-FiscalSurvey-AfDB-SDMX/1.0",
    "Accept": "application/xml,text/xml,application/vnd.sdmx.structurespecificdata+xml,*/*",
    "Referer": HOST + "/",
})
_VERIFY_SSL = True
_SSL_NOTICE_SHOWN = False

# =====================================================================
# 4. GENERIC HELPERS
# =====================================================================
def _clean_code(x) -> str:
    s = "" if x is None else str(x).strip()
    return re.sub(r"\.0$", "", s)

def _norm(x) -> str:
    s = "" if x is None else str(x)
    s = s.replace("\ufeff", " ")
    s = re.sub(r"[_\-/]+", " ", s.lower())
    s = re.sub(r"[^\w\s.%$()]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _local_name(tag: str) -> str:
    return str(tag).split("}")[-1].split(":")[-1]

def _project_root() -> Path:
    return Path(__file__).resolve().parent

def _load_locked_registry(root: Path) -> pd.DataFrame:
    path = root / "mapping_registry" / "AFDB_LOCKED.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Missing required registry: {path}\n"
            "Keep the cleaned 9-row AFDB_LOCKED.csv in mapping_registry."
        )

    reg = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    required = {"dataset_id", "indicator_code", "wbfd_mnemonic"}
    missing = required - set(reg.columns)
    if missing:
        raise RuntimeError(
            f"AFDB_LOCKED.csv missing columns {sorted(missing)}; "
            f"found={list(reg.columns)}"
        )

    reg["dataset_id"] = reg["dataset_id"].map(_clean_code)
    reg["indicator_code"] = reg["indicator_code"].map(_clean_code)
    reg["wbfd_mnemonic"] = reg["wbfd_mnemonic"].astype(str).str.strip().str.upper()

    approved_pairs = {
        (code, rec["wbfd_mnemonic"])
        for code, rec in EXPECTED_LOCKED.items()
    }

    reg = reg[
        reg.apply(
            lambda r: (
                r["dataset_id"] == DATASET_ID
                and (r["indicator_code"], r["wbfd_mnemonic"]) in approved_pairs
            ),
            axis=1,
        )
    ].copy()

    found_pairs = set(zip(reg["indicator_code"], reg["wbfd_mnemonic"]))
    missing_pairs = approved_pairs - found_pairs
    if missing_pairs:
        raise RuntimeError(
            "AFDB_LOCKED.csv does not contain all 9 approved exact mappings. "
            f"Missing={sorted(missing_pairs)}"
        )

    reg = reg.drop_duplicates(
        ["dataset_id", "indicator_code", "wbfd_mnemonic"]
    ).reset_index(drop=True)

    if len(reg) != 9:
        raise RuntimeError(
            f"Expected exactly 9 approved mappings after validation; got {len(reg)}."
        )
    return reg

# =====================================================================
# 5. PRIMARY PATH: EXACT SDMX API EXPOSED BY THE DATA FINDER UI
# =====================================================================
def _sdmx_url(start_year: int, end_year: int) -> str:
    country_key = "+".join(_ALL_COUNTRY_MEMBER_KEYS)
    indicator_key = "+".join(sorted(EXPECTED_CODES))
    key = f"{country_key}.{indicator_key}"
    return (
        f"{HOST}/api/1.0/sdmx/data/{DATASET_ID}"
        f"?key={key}&startPeriod={start_year}&endPeriod={end_year}"
    )


def _sdmx_url_for_indicator(indicator_code: str, start_year: int, end_year: int) -> str:
    """
    Query exactly one AfDB SDMX indicator member at a time.

    Why V6 does this:
    The AfDB StructureSpecificData response does not necessarily echo the
    numeric SDMX member ID used in the request. By requesting one member at a
    time, the requested LOCKED source code is known unambiguously and does not
    need to be rediscovered from the XML.
    """
    indicator_code = _clean_code(indicator_code)
    if indicator_code not in EXPECTED_CODES:
        raise ValueError(f"Unexpected AfDB LOCKED indicator: {indicator_code}")

    country_key = "+".join(_ALL_COUNTRY_MEMBER_KEYS)
    key = f"{country_key}.{indicator_code}"

    return (
        f"{HOST}/api/1.0/sdmx/data/{DATASET_ID}"
        f"?key={key}&startPeriod={start_year}&endPeriod={end_year}"
    )

def _request_sdmx(url: str, timeout: int = 900) -> requests.Response:
    global _VERIFY_SSL, _SSL_NOTICE_SHOWN

    try:
        r = _SESSION.get(url, timeout=timeout, verify=_VERIFY_SSL)
    except requests.exceptions.SSLError:
        _VERIFY_SSL = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        if not _SSL_NOTICE_SHOWN:
            print(
                "    AfDB certificate chain is not trusted locally; "
                "retrying this official host with verify=False."
            )
            _SSL_NOTICE_SHOWN = True
        r = _SESSION.get(url, timeout=timeout, verify=False)

    content_type = (r.headers.get("content-type") or "").lower()
    preview = r.text[:1200] if r.content else ""

    if r.status_code != 200:
        if (
            r.status_code == 403
            and (
                "just a moment" in preview.lower()
                or "challenges.cloudflare.com" in preview.lower()
                or "verifying you are human" in preview.lower()
            )
        ):
            raise RuntimeError(
                "CLOUDFLARE_BROWSER_CHALLENGE: exact SDMX endpoint was requested, "
                "but Cloudflare blocked the non-browser HTTP request before it reached "
                "the SDMX service."
            )
        raise RuntimeError(
            f"AfDB SDMX HTTP {r.status_code}; content-type={content_type}; "
            f"response={preview[:700]}"
        )

    if not r.content:
        raise RuntimeError("AfDB SDMX returned HTTP 200 with an empty body.")

    if (
        "html" in content_type
        or "<html" in preview.lower()
        or "just a moment" in preview.lower()
        or "verifying you are human" in preview.lower()
    ):
        raise RuntimeError(
            "CLOUDFLARE_BROWSER_CHALLENGE: SDMX request returned HTML instead of XML."
        )

    return r

# ---------------------------------------------------------------------
# SDMX parser V5
#
# Supports:
#   - SDMX 2.1 StructureSpecificData
#   - SDMX GenericData with SeriesKey/Value + ObsDimension/ObsValue
#   - SDMX 2.0 Compact-style Series/Obs
#   - flat observation layouts where dimensions sit directly on Obs
#
# Important: the AfDB UI-generated SDMX request uses 84 country/aggregate
# members. We request those exact members, then retain sovereign countries
# during final QC.
# ---------------------------------------------------------------------

API_COUNTRY_TO_OUTPUT = {
    api_code: COUNTRY_CODE_NORMALIZATION.get(api_code, api_code)
    for api_code in SOVEREIGN_AFRICA_API_CODES
}
OUTPUT_SOVEREIGN_CODES = set(API_COUNTRY_TO_OUTPUT.values())

COUNTRY_NAME_TO_OUTPUT = {
    "algeria": "DZA",
    "angola": "AGO",
    "benin": "BEN",
    "botswana": "BWA",
    "burkina faso": "BFA",
    "burundi": "BDI",
    "cabo verde": "CPV",
    "cape verde": "CPV",
    "cameroon": "CMR",
    "central african republic": "CAF",
    "chad": "TCD",
    "comoros": "COM",
    "congo": "COG",
    "republic of the congo": "COG",
    "congo republic": "COG",
    "democratic republic of the congo": "COD",
    "democratic republic of congo": "COD",
    "dr congo": "COD",
    "drc": "COD",
    "cote d ivoire": "CIV",
    "côte d ivoire": "CIV",
    "ivory coast": "CIV",
    "djibouti": "DJI",
    "egypt": "EGY",
    "equatorial guinea": "GNQ",
    "eritrea": "ERI",
    "eswatini": "SWZ",
    "swaziland": "SWZ",
    "ethiopia": "ETH",
    "gabon": "GAB",
    "gambia": "GMB",
    "the gambia": "GMB",
    "ghana": "GHA",
    "guinea": "GIN",
    "guinea bissau": "GNB",
    "kenya": "KEN",
    "lesotho": "LSO",
    "liberia": "LBR",
    "libya": "LBY",
    "madagascar": "MDG",
    "malawi": "MWI",
    "mali": "MLI",
    "mauritania": "MRT",
    "mauritius": "MUS",
    "morocco": "MAR",
    "mozambique": "MOZ",
    "namibia": "NAM",
    "niger": "NER",
    "nigeria": "NGA",
    "rwanda": "RWA",
    "sao tome and principe": "STP",
    "são tomé and príncipe": "STP",
    "senegal": "SEN",
    "seychelles": "SYC",
    "sierra leone": "SLE",
    "somalia": "SOM",
    "south africa": "ZAF",
    "south sudan": "SSD",
    "sudan": "SDN",
    "tanzania": "TZA",
    "togo": "TGO",
    "tunisia": "TUN",
    "uganda": "UGA",
    "zambia": "ZMB",
    "zimbabwe": "ZWE",
}

INDICATOR_LABEL_ALIASES = {
    "1000390": {
        "direct taxes",
        "direct taxes on income profits",
        "direct taxes on income and profits",
    },
    "1000310": {
        "current expenditure",
        "central government current expenditure loc curr",
        "central government current expenditure local currency",
    },
    "1000380": {
        "wages and salaries",
        "central government current expenditure wages and salaries loc curr",
        "central government current expenditure wages and salaries local currency",
    },
    "1005810": {"interest payments"},
    "1005800": {"other current expenditure"},
    "1000300": {
        "capital expenditure",
        "central government capital expenditure loc curr",
        "central government capital expenditure local currency",
    },
    "1001860": {
        "total external public debt",
        "total external public debt current usd",
    },
    "1001900": {
        "total government domestic debt",
        "total government domestic debt in local currency",
    },
    "1002130": {
        "total debt interest paid",
        "total debt interest paid cur usd",
        "total debt interest paid current usd",
    },
}


def _norm_sdmx_value(x) -> str:
    s = "" if x is None else str(x)
    s = s.replace("\ufeff", " ").replace("’", "'")
    s = s.lower()
    s = re.sub(r"[_/|,:;()\[\]\-]+", " ", s)
    s = re.sub(r"[^a-z0-9\u00c0-\u024f\s]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _context_values(ctx: dict):
    vals = []
    for k, v in ctx.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            vals.extend(str(x).strip() for x in v if x is not None)
        else:
            vals.append(str(v).strip())
    return [x for x in vals if x]


def _dimension_key_matches(key, concepts):
    nk = _norm_sdmx_value(key)
    return any(
        nk == c or nk.startswith(c + " ") or nk.endswith(" " + c) or c in nk
        for c in concepts
    )


def _indicator_from_single_value(value):
    if value is None:
        return None
    s = str(value).strip()
    cleaned = _clean_code(s)
    if cleaned in EXPECTED_CODES:
        return cleaned
    for m in re.findall(r"(?<!\\d)(\\d{7})(?!\\d)", s):
        if m in EXPECTED_CODES:
            return m
    nv = _norm_sdmx_value(s)
    for code, aliases in INDICATOR_LABEL_ALIASES.items():
        if nv in aliases:
            return code
    return None


def _resolve_indicator(ctx: dict):
    indicator_concepts = {
        "indicator", "indicators", "subject", "series", "item",
        "variable", "measure"
    }

    # Concept-aware resolution first. AfDB/Knoema numeric member IDs overlap
    # across dimensions, so the dimension name must take precedence.
    for key, value in ctx.items():
        if _dimension_key_matches(key, indicator_concepts):
            code = _indicator_from_single_value(value)
            if code:
                return code

    # Fallback for Compact/structure-specific variants where dimension names
    # are opaque but the exact approved source ID appears in context.
    for value in _context_values(ctx):
        code = _indicator_from_single_value(value)
        if code:
            return code

    return None


def _country_from_single_value(value):
    if value is None:
        return None, None
    s = str(value).strip()
    cleaned = _clean_code(s)

    if cleaned in _INTERNAL_TO_API_COUNTRY:
        api_code = _INTERNAL_TO_API_COUNTRY[cleaned]
        if api_code in SOVEREIGN_AFRICA_API_CODES:
            return COUNTRY_CODE_NORMALIZATION.get(api_code, api_code), cleaned
        return None, cleaned

    upper = s.upper()
    if upper in SOVEREIGN_AFRICA_API_CODES:
        return COUNTRY_CODE_NORMALIZATION.get(upper, upper), upper

    nv = _norm_sdmx_value(s)
    if nv in COUNTRY_NAME_TO_OUTPUT:
        return COUNTRY_NAME_TO_OUTPUT[nv], s

    return None, None


def _resolve_country(ctx: dict):
    country_concepts = {
        "country", "countries", "economy", "economies", "region",
        "location", "ref area", "ref_area", "geo", "geography"
    }

    # Concept-aware resolution first to avoid confusing an indicator member
    # ID (e.g. 1000390) with a country member ID from another dimension.
    for key, value in ctx.items():
        if _dimension_key_matches(key, country_concepts):
            country, source = _country_from_single_value(value)
            if country:
                return country, source
            # If the explicit country dimension is an aggregate, do not allow
            # another dimension's overlapping numeric ID to masquerade as a country.
            if _clean_code(value) in _INTERNAL_TO_API_COUNTRY:
                return None, _clean_code(value)

    # Fallback only when no recognizable country concept is present.
    for key, value in ctx.items():
        if _dimension_key_matches(
            key,
            {"indicator", "indicators", "subject", "series", "item", "variable", "measure"},
        ):
            continue
        country, source = _country_from_single_value(value)
        if country:
            return country, source

    return None, None


def _add_value_element_to_context(ctx: dict, elem: ET.Element):
    local = _local_name(elem.tag).lower()
    if local != "value":
        return

    concept = (
        elem.attrib.get("id")
        or elem.attrib.get("concept")
        or elem.attrib.get("Concept")
        or elem.attrib.get("conceptRef")
        or elem.attrib.get("name")
        or "Value"
    )
    value = (
        elem.attrib.get("value")
        or elem.attrib.get("Value")
        or elem.attrib.get("code")
        or elem.text
    )

    if value is not None:
        ctx[str(concept)] = str(value)


def _context_for_observation(obs: ET.Element, parent_map: dict):
    ctx = {}

    # Observation attributes themselves may include all dimensions in a flat
    # response.
    for k, v in obs.attrib.items():
        ctx[_local_name(k)] = v

    node = obs
    depth = 0

    while node is not None and depth < 12:
        # Ancestor attributes: Series attributes in StructureSpecific/Compact.
        for k, v in node.attrib.items():
            key = _local_name(k)
            if key not in ctx:
                ctx[key] = v

        # SeriesKey / GroupKey / direct Value children in Generic SDMX.
        for child in list(node):
            clocal = _local_name(child.tag).lower()

            if clocal == "value":
                _add_value_element_to_context(ctx, child)

            elif clocal in {
                "serieskey", "groupkey", "keys", "key",
                "attributes", "seriesattributes", "groupattributes"
            }:
                for sub in child.iter():
                    if _local_name(sub.tag).lower() == "value":
                        _add_value_element_to_context(ctx, sub)

        node = parent_map.get(node)
        depth += 1

    return ctx


def _obs_period_and_value(obs: ET.Element):
    attrs = {_local_name(k): v for k, v in obs.attrib.items()}

    period = (
        attrs.get("TIME_PERIOD")
        or attrs.get("TimePeriod")
        or attrs.get("time_period")
        or attrs.get("TIME")
        or attrs.get("time")
        or attrs.get("period")
        or attrs.get("Period")
    )

    value = (
        attrs.get("OBS_VALUE")
        or attrs.get("ObsValue")
        or attrs.get("obs_value")
        or attrs.get("VALUE")
        or attrs.get("value")
    )

    for child in obs.iter():
        local = _local_name(child.tag).lower()

        if local in {
            "observationdimension", "obsdimension", "time",
            "timeperiod", "time_period", "period"
        }:
            if period is None:
                period = (
                    child.attrib.get("value")
                    or child.attrib.get("Value")
                    or child.text
                )

        elif local in {"obsvalue", "observationvalue"}:
            if value is None:
                value = (
                    child.attrib.get("value")
                    or child.attrib.get("Value")
                    or child.text
                )

        elif local == "value":
            concept = _norm_sdmx_value(
                child.attrib.get("id")
                or child.attrib.get("concept")
                or child.attrib.get("Concept")
                or ""
            )
            val = (
                child.attrib.get("value")
                or child.attrib.get("Value")
                or child.text
            )

            if period is None and (
                "time" in concept or "period" in concept
            ):
                period = val

            if value is None and (
                "obs value" in concept
                or concept in {"value", "observation value"}
            ):
                value = val

    return period, value


def _sdmx_diagnostics(root: ET.Element, content: bytes):
    from collections import Counter

    counts = Counter()
    attr_samples = []
    text_samples = []

    for elem in root.iter():
        local = _local_name(elem.tag)
        counts[local] += 1

        if len(attr_samples) < 40 and elem.attrib:
            attr_samples.append({
                "tag": local,
                "attrs": {
                    _local_name(k): str(v)[:200]
                    for k, v in elem.attrib.items()
                },
            })

        if len(text_samples) < 20:
            txt = (elem.text or "").strip()
            if txt:
                text_samples.append({
                    "tag": local,
                    "text": txt[:300],
                })

    decoded = content.decode("utf-8", errors="replace")

    return {
        "root_tag": _local_name(root.tag),
        "element_counts": dict(counts.most_common(30)),
        "expected_indicator_ids_present_in_xml": sorted(
            code for code in EXPECTED_CODES if code in decoded
        ),
        "requested_country_member_ids_present_count": sum(
            1 for code in _ALL_COUNTRY_MEMBER_KEYS if code in decoded
        ),
        "attribute_samples": attr_samples,
        "text_samples": text_samples,
        "response_preview": decoded[:3000],
    }


def _parse_sdmx_xml(
    content: bytes,
    diagnostic_path=None,
    forced_indicator_code: str | None = None,
) -> pd.DataFrame:
    """
    Parse AfDB SDMX XML.

    V6 key behavior:
    If forced_indicator_code is supplied, the response came from a
    one-indicator SDMX request. We therefore do NOT require the numeric source
    indicator ID to appear again in the XML. Every parsed observation is
    assigned to the exact requested LOCKED code.

    This avoids the V5 failure where valid StructureSpecificData contained
    485 Series / 4635 Obs but used a different public indicator representation
    in the returned XML.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        preview = content[:1500].decode("utf-8", errors="replace")
        raise RuntimeError(
            f"AfDB response was not valid XML: {e}. Preview={preview}"
        ) from e

    if forced_indicator_code is not None:
        forced_indicator_code = _clean_code(forced_indicator_code)
        if forced_indicator_code not in EXPECTED_CODES:
            raise RuntimeError(
                f"Parser received non-LOCKED forced indicator: {forced_indicator_code}"
            )

    diagnostics = _sdmx_diagnostics(root, content)
    diagnostics["forced_indicator_code"] = forced_indicator_code

    if diagnostic_path is not None:
        try:
            Path(diagnostic_path).write_text(
                json.dumps(diagnostics, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    parent_map = {
        child: parent
        for parent in root.iter()
        for child in list(parent)
    }

    rows = []
    obs_elements = [
        elem for elem in root.iter()
        if _local_name(elem.tag).lower() in {"obs", "observation"}
    ]

    for obs in obs_elements:
        ctx = _context_for_observation(obs, parent_map)

        indicator_code = forced_indicator_code or _resolve_indicator(ctx)
        country_code, country_source = _resolve_country(ctx)

        if not indicator_code or not country_code:
            continue

        period, value = _obs_period_and_value(obs)
        if period is None or value is None:
            continue

        year_match = re.search(r"((?:19|20)\d{2})", str(period))
        if not year_match:
            continue

        try:
            numeric_value = float(
                str(value).replace(",", "").replace("\u00a0", "").strip()
            )
        except Exception:
            continue

        frequency = (
            ctx.get("FREQ")
            or ctx.get("freq")
            or ctx.get("Frequency")
            or ctx.get("frequency")
            or "A"
        )

        unit_original = (
            ctx.get("UNIT")
            or ctx.get("unit")
            or ctx.get("UNIT_MEASURE")
            or ctx.get("unit_measure")
            or ctx.get("Unit")
            or ""
        )

        # Keep the returned API/public indicator representation for audit.
        response_indicator = ""
        for key, val in ctx.items():
            nk = _norm_sdmx_value(key)
            if any(
                token in nk
                for token in ("indicator", "subject", "series", "item", "variable", "measure")
            ):
                sval = str(val).strip()
                # Don't store the already-known numeric requested key as the
                # response mnemonic if the XML happened to echo it.
                if sval:
                    response_indicator = sval
                    break

        rows.append({
            "dataset_id": DATASET_ID,
            "country_original": country_code,
            "country_source_code": country_source,
            "indicator_code": indicator_code,
            "indicator_name": EXPECTED_LOCKED[indicator_code]["indicator_original"],
            "indicator_response_code": response_indicator,
            "TIME_PERIOD": str(period),
            "OBS_VALUE": numeric_value,
            "unit_original": str(unit_original or ""),
            "scale_value": 1.0,
            "frequency_original": str(frequency or "A"),
            "year": int(year_match.group(1)),
        })

    raw = pd.DataFrame(rows)

    if raw.empty:
        counts = diagnostics.get("element_counts", {})
        ind_found = diagnostics.get("expected_indicator_ids_present_in_xml", [])
        country_n = diagnostics.get("requested_country_member_ids_present_count", 0)

        error_text = ""
        for item in diagnostics.get("text_samples", []):
            tag = str(item.get("tag", "")).lower()
            if any(x in tag for x in ("error", "text", "message", "status")):
                error_text += f" {item.get('text', '')}"
        error_text = error_text.strip()[:800]

        raise RuntimeError(
            "AfDB returned valid XML, but no usable country/year/value observations "
            f"could be parsed for requested indicator={forced_indicator_code}. "
            f"root={diagnostics.get('root_tag')}; "
            f"Series={counts.get('Series', counts.get('series', 0))}; "
            f"Obs={counts.get('Obs', counts.get('obs', 0))}; "
            f"numeric LOCKED IDs echoed in XML={len(ind_found)}/9; "
            f"internal country member IDs echoed={country_n}/84."
            + (f" XML message: {error_text}" if error_text else "")
            + (
                f" Diagnostic saved to {diagnostic_path}."
                if diagnostic_path is not None else ""
            )
        )

    return raw



def _try_sdmx_direct(start_year: int, end_year: int, data_dir: Path, meta_dir: Path):
    """
    Direct HTTP path, one LOCKED indicator per request.
    Stops immediately on Cloudflare and lets the normal-browser bridge take over.
    """
    frames = []
    audit = []
    urls = []

    ordered_codes = [
        code for code in EXPECTED_LOCKED.keys()
    ]

    for idx, indicator_code in enumerate(ordered_codes, 1):
        url = _sdmx_url_for_indicator(indicator_code, start_year, end_year)
        urls.append(url)

        print(
            f"    SDMX direct [{idx}/9] {indicator_code} "
            f"({EXPECTED_LOCKED[indicator_code]['indicator_original']})"
        )

        t0 = time.time()
        response = _request_sdmx(url)
        elapsed = time.time() - t0

        xml_path = (
            data_dir
            / f"afdb_nbyenxf_sdmx_{indicator_code}_direct.xml"
        )
        xml_path.write_bytes(response.content)

        raw_i = _parse_sdmx_xml(
            response.content,
            diagnostic_path=(
                meta_dir
                / f"sdmx_diagnostics_{indicator_code}_direct.json"
            ),
            forced_indicator_code=indicator_code,
        )

        raw_i = raw_i[
            raw_i["year"].ge(start_year)
            & raw_i["year"].le(end_year)
            & raw_i["OBS_VALUE"].notna()
        ].copy()

        if raw_i.empty:
            raise RuntimeError(
                f"AfDB direct SDMX returned no usable rows for {indicator_code}."
            )

        frames.append(raw_i)
        audit.append({
            "indicator_code": indicator_code,
            "indicator_name": EXPECTED_LOCKED[indicator_code]["indicator_original"],
            "rows": len(raw_i),
            "countries": raw_i["country_original"].nunique(),
            "min_year": int(raw_i["year"].min()),
            "max_year": int(raw_i["year"].max()),
            "http_status": response.status_code,
            "response_bytes": len(response.content),
            "elapsed_seconds": round(elapsed, 3),
            "url": url,
        })

    (meta_dir / "sdmx_request_urls_per_indicator.txt").write_text(
        "\n".join(urls),
        encoding="utf-8",
    )

    audit_df = pd.DataFrame(audit)
    audit_df.to_csv(
        meta_dir / "sdmx_per_indicator_direct_audit.csv",
        index=False,
        encoding="utf-8-sig",
    )

    raw = pd.concat(frames, ignore_index=True, sort=False)

    observed = set(raw["indicator_code"].astype(str))
    missing = EXPECTED_CODES - observed
    if missing:
        raise RuntimeError(
            f"AfDB direct per-indicator SDMX is missing: {sorted(missing)}"
        )

    return raw, {
        "retrieval_mode": "SDMX_DIRECT_PER_INDICATOR",
        "source_input": str(meta_dir / "sdmx_request_urls_per_indicator.txt"),
        "http_status": 200,
        "response_bytes": int(audit_df["response_bytes"].sum()),
        "elapsed_seconds": round(float(audit_df["elapsed_seconds"].sum()), 3),
        "raw_xml_file": str(data_dir),
    }


# =====================================================================
# 6. FALLBACK PATH: NORMAL BROWSER OFFICIAL CSV/XLSX EXPORT
# =====================================================================
COUNTRY_ALIASES = {
    "country", "country name", "economy", "economy name",
    "location", "region", "ref area", "ref_area", "geo", "geography",
}
YEAR_ALIASES = {"year", "date", "time", "time period", "time_period", "period"}
VALUE_ALIASES = {"value", "obs value", "obs_value", "observation", "data", "amount"}
UNIT_ALIASES = {"unit", "units", "unit original", "unit_original"}
INDICATOR_ALIASES = {
    "indicator", "indicator code", "indicator_code", "indicator name",
    "indicator_name", "series", "subject", "item", "variable", "measure",
}

def _supported_export_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in {".csv", ".xlsx", ".xls"}
        and not path.name.startswith("~$")
        and not path.name.endswith((".crdownload", ".part", ".tmp"))
    )

def _stable_file(path: Path, checks: int = 3, pause: float = 0.7) -> bool:
    try:
        last = None
        for _ in range(checks):
            size = path.stat().st_size
            if size <= 0:
                return False
            if last is not None and size != last:
                last = size
                time.sleep(pause)
                continue
            last = size
            time.sleep(pause)
        return path.exists() and path.stat().st_size == last
    except OSError:
        return False

def _read_variants(path: Path):
    frames = []

    if path.suffix.lower() == ".csv":
        for header in range(0, 6):
            try:
                df = pd.read_csv(
                    path,
                    header=header,
                    sep=None,
                    engine="python",
                    encoding="utf-8-sig",
                    on_bad_lines="skip",
                )
                if not df.empty and len(df.columns) >= 2:
                    frames.append((f"csv_header_{header}", df))
            except Exception:
                continue
        return frames

    try:
        xls = pd.ExcelFile(path)
    except Exception:
        return frames

    for sheet in xls.sheet_names:
        for header in range(0, 6):
            try:
                df = pd.read_excel(path, sheet_name=sheet, header=header)
                if not df.empty and len(df.columns) >= 2:
                    df["__source_sheet__"] = sheet
                    frames.append((f"xlsx_{sheet}_header_{header}", df))
            except Exception:
                continue
    return frames

def _find_col(df: pd.DataFrame, aliases: set[str]):
    by_norm = {_norm(c): c for c in df.columns}
    for alias in aliases:
        if alias in by_norm:
            return by_norm[alias]

    for c in df.columns:
        nc = _norm(c)
        if any(
            nc == a
            or nc.startswith(a + " ")
            or nc.endswith(" " + a)
            for a in aliases
        ):
            return c
    return None

def _extract_code_from_text(text):
    s = "" if text is None else str(text)
    for code in EXPECTED_CODES:
        if re.search(rf"(?<!\d){re.escape(code)}(?!\d)", s):
            return code
    return None

def _label_to_code_exact(text):
    nt = _norm(text)
    if not nt:
        return None

    # Exact approved labels only.
    exact_labels = {
        code: {
            _norm(rec["indicator_original"]),
        }
        for code, rec in EXPECTED_LOCKED.items()
    }

    for code, labels in exact_labels.items():
        if nt in labels:
            return code
    return None

def _row_indicator_codes(df: pd.DataFrame) -> pd.Series:
    cols = [c for c in df.columns if _norm(c) in INDICATOR_ALIASES]
    if not cols:
        cols = list(df.select_dtypes(include=["object", "string"]).columns)

    out = []
    for _, row in df[cols].iterrows():
        code = None
        for v in row:
            code = _extract_code_from_text(v)
            if code:
                break
        if not code:
            for v in row:
                code = _label_to_code_exact(v)
                if code:
                    break
        out.append(code)

    return pd.Series(out, index=df.index, dtype="object")

def _parse_row_dimension_export(df: pd.DataFrame, variant: str):
    work = df.copy().dropna(how="all").dropna(axis=1, how="all")
    if work.empty:
        return None

    work["__indicator_code__"] = _row_indicator_codes(work)
    work = work[work["__indicator_code__"].isin(EXPECTED_CODES)].copy()
    if work.empty:
        return None

    country_col = _find_col(work, COUNTRY_ALIASES)
    unit_col = _find_col(work, UNIT_ALIASES)
    if not country_col:
        return None

    year_col = _find_col(work, YEAR_ALIASES)
    value_col = _find_col(work, VALUE_ALIASES)

    parts = []

    if year_col and value_col and year_col != value_col:
        cols = [country_col, "__indicator_code__", year_col, value_col]
        if unit_col:
            cols.append(unit_col)

        tmp = work[cols].copy()
        tmp["year"] = pd.to_numeric(
            tmp[year_col].astype("string").str.extract(
                r"((?:19|20)\d{2})", expand=False
            ),
            errors="coerce",
        )
        tmp["OBS_VALUE"] = pd.to_numeric(tmp[value_col], errors="coerce")
        tmp["unit_original"] = tmp[unit_col].astype(str) if unit_col else ""
        tmp["country_original"] = tmp[country_col].astype(str).str.strip()
        tmp["indicator_code"] = tmp["__indicator_code__"]
        parts.append(
            tmp[
                [
                    "country_original",
                    "indicator_code",
                    "year",
                    "OBS_VALUE",
                    "unit_original",
                ]
            ]
        )

    year_columns = []
    for c in work.columns:
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(c))
        if m:
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                year_columns.append((c, y))

    if year_columns:
        id_cols = [country_col, "__indicator_code__"]
        if unit_col:
            id_cols.append(unit_col)

        tmp = work[id_cols + [c for c, _ in year_columns]].copy()
        rename_year = {c: str(y) for c, y in year_columns}
        tmp = tmp.rename(columns=rename_year)

        long = tmp.melt(
            id_vars=id_cols,
            value_vars=[str(y) for _, y in year_columns],
            var_name="year",
            value_name="OBS_VALUE",
        )
        long["year"] = pd.to_numeric(long["year"], errors="coerce")
        long["OBS_VALUE"] = pd.to_numeric(long["OBS_VALUE"], errors="coerce")
        long["unit_original"] = long[unit_col].astype(str) if unit_col else ""
        long["country_original"] = long[country_col].astype(str).str.strip()
        long["indicator_code"] = long["__indicator_code__"]

        parts.append(
            long[
                [
                    "country_original",
                    "indicator_code",
                    "year",
                    "OBS_VALUE",
                    "unit_original",
                ]
            ]
        )

    if not parts:
        return None

    out = pd.concat(parts, ignore_index=True, sort=False)
    out = out.dropna(subset=["year", "OBS_VALUE"])
    out = out[
        out["country_original"].notna()
        & out["country_original"].astype(str).str.strip().ne("")
    ]
    if out.empty:
        return None

    return out, {"variant": variant, "layout": "row_dimension"}

def _series_header_code(header: str):
    code = _extract_code_from_text(header)
    if code:
        return code

    nh = _norm(header)
    for code, rec in EXPECTED_LOCKED.items():
        nl = _norm(rec["indicator_original"])
        if nl and nl in nh:
            return code
    return None

def _country_from_series_header(header: str, code: str):
    s = str(header)
    s = re.sub(rf"(?<!\d){re.escape(code)}(?!\d)", " ", s)

    label = EXPECTED_LOCKED[code]["indicator_original"]
    s = re.sub(re.escape(label), " ", s, flags=re.I)

    s = re.sub(
        r"\b(annual|yearly|frequency|indicator|value|current usd|local currency|loc\.?\s*curr\.?)\b",
        " ",
        s,
        flags=re.I,
    )
    s = re.sub(r"[\|\[\]\(\)_;:,]+", " ", s)
    s = re.sub(r"\s+-\s+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -")

def _parse_series_columns_export(df: pd.DataFrame, variant: str):
    work = df.copy().dropna(how="all").dropna(axis=1, how="all")
    if work.empty:
        return None

    year_col = _find_col(work, YEAR_ALIASES)
    if not year_col:
        first = work.columns[0]
        probe = work[first].astype("string").str.extract(
            r"((?:19|20)\d{2})", expand=False
        )
        if probe.notna().mean() >= 0.5:
            year_col = first

    if not year_col:
        return None

    years = pd.to_numeric(
        work[year_col].astype("string").str.extract(
            r"((?:19|20)\d{2})", expand=False
        ),
        errors="coerce",
    )

    rows = []
    for c in work.columns:
        if c == year_col or c == "__source_sheet__":
            continue

        code = _series_header_code(str(c))
        if not code:
            continue

        vals = pd.to_numeric(work[c], errors="coerce")
        valid = years.notna() & vals.notna()
        if not valid.any():
            continue

        country = _country_from_series_header(str(c), code)
        if not country:
            continue

        rows.append(
            pd.DataFrame({
                "country_original": country,
                "indicator_code": code,
                "year": years[valid].astype(int),
                "OBS_VALUE": vals[valid],
                "unit_original": "",
            })
        )

    if not rows:
        return None

    return (
        pd.concat(rows, ignore_index=True),
        {"variant": variant, "layout": "series_columns"},
    )

def _parse_official_export(path: Path, start_year: int, end_year: int):
    variants = _read_variants(path)
    if not variants:
        raise RuntimeError(f"Could not read official CSV/XLSX export: {path}")

    best = None
    errors = []

    for variant, df in variants:
        for parser in (_parse_row_dimension_export, _parse_series_columns_export):
            try:
                result = parser(df, variant)
                if result is None:
                    continue

                parsed, info = result
                parsed = parsed[
                    parsed["year"].ge(start_year)
                    & parsed["year"].le(end_year)
                ].copy()

                if parsed.empty:
                    continue

                observed = set(parsed["indicator_code"].astype(str))
                score = len(observed & EXPECTED_CODES)
                candidate = (score, len(parsed), parsed, info)

                if best is None or candidate[:2] > best[:2]:
                    best = candidate

            except Exception as e:
                errors.append(
                    f"{variant}/{parser.__name__}: {type(e).__name__}: {e}"
                )

    if best is None:
        raise RuntimeError(
            "The file was readable, but the AfDB country/indicator/year/value "
            "layout could not be recognized. "
            f"Parser notes: {' | '.join(errors[:8])}"
        )

    _, _, out, info = best

    observed = set(out["indicator_code"].astype(str))
    missing = EXPECTED_CODES - observed
    if missing:
        raise RuntimeError(
            "Official AfDB export does not contain all 9 LOCKED indicators. "
            f"Observed={sorted(observed)}; Missing={sorted(missing)}"
        )

    out = out[out["indicator_code"].isin(EXPECTED_CODES)].copy()
    out["dataset_id"] = DATASET_ID
    out["indicator_name"] = out["indicator_code"].map(
        lambda c: EXPECTED_LOCKED[str(c)]["indicator_original"]
    )
    out["country_source_code"] = out["country_original"].astype(str)
    out["TIME_PERIOD"] = out["year"].astype("Int64").astype(str)
    out["scale_value"] = 1.0
    out["frequency_original"] = "A"

    return out, info

def _download_dirs(root: Path):
    project_dir = root / "afdb_official_downloads"
    project_dir.mkdir(parents=True, exist_ok=True)

    dirs = [project_dir, Path.home() / "Downloads", Path.home() / "downloads"]

    unique = []
    seen = set()
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            d.mkdir(parents=True, exist_ok=True)
            unique.append(d)
            seen.add(key)
    return unique

def _ordered_export_candidates(root: Path, dirs, new_after=None):
    paths = []

    explicit = os.environ.get("AFDB_OFFICIAL_EXPORT_PATH", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists() and _supported_export_file(p):
            paths.append(p)

    project_dir = root / "afdb_official_downloads"
    if project_dir.exists():
        paths.extend(
            p for p in project_dir.iterdir()
            if _supported_export_file(p)
        )

    for d in dirs:
        if d.resolve() == project_dir.resolve():
            continue

        for p in d.iterdir():
            if not _supported_export_file(p):
                continue

            if new_after is not None:
                try:
                    if p.stat().st_mtime < new_after:
                        continue
                except OSError:
                    continue

            paths.append(p)

    unique = {}
    for p in paths:
        try:
            unique[str(p.resolve())] = p
        except OSError:
            pass

    return sorted(
        unique.values(),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )

def _try_export_candidates(candidates, start_year, end_year, audit_rows):
    for p in candidates:
        if not _stable_file(p):
            audit_rows.append({
                "path": str(p),
                "status": "NOT_STABLE_OR_EMPTY",
                "message": "",
            })
            continue

        try:
            raw, info = _parse_official_export(p, start_year, end_year)
            audit_rows.append({
                "path": str(p),
                "status": "VALID",
                "message": f"{info.get('variant')} / {info.get('layout')}",
                "rows": len(raw),
                "indicators": raw["indicator_code"].nunique(),
                "countries": raw["country_original"].nunique(),
                "min_year": int(raw["year"].min()),
                "max_year": int(raw["year"].max()),
            })
            return p, raw, info

        except Exception as e:
            audit_rows.append({
                "path": str(p),
                "status": "REJECTED",
                "message": str(e)[:1500],
            })

    return None, None, None

def _browser_export_fallback(root, start_year, end_year, audit_rows):
    dirs = _download_dirs(root)

    # Use an already-downloaded valid export if available.
    candidates = _ordered_export_candidates(root, dirs)
    chosen, raw, info = _try_export_candidates(
        candidates, start_year, end_year, audit_rows
    )
    if chosen is not None:
        return raw, {
            "retrieval_mode": "OFFICIAL_BROWSER_EXPORT_EXISTING",
            "source_input": str(chosen),
            "parse_variant": info.get("variant", ""),
            "parse_layout": info.get("layout", ""),
        }

    wait_seconds = int(os.environ.get("AFDB_BROWSER_WAIT_SECONDS", "900"))
    dataset_page = os.environ.get("AFDB_DATASET_URL", DATASET_PAGE).strip()
    started = time.time()

    print("\n  SDMX direct path was unavailable.")
    print("  Opening the official AfDB Data Finder page in your NORMAL browser.")
    print("  Please use Download -> CSV (preferred) or Excel.")
    print("  You do NOT need to move or rename the file.")
    print("  The collector will watch your Downloads folder automatically.")
    print(f"  Wait timeout: {wait_seconds} seconds")

    webbrowser.open(dataset_page, new=2)

    seen = set()
    last_notice = 0.0

    while True:
        now = time.time()
        if now - started > wait_seconds:
            raise RuntimeError(
                f"Timed out after {wait_seconds} seconds waiting for an official "
                "AfDB CSV/XLSX export. You can alternatively set "
                "AFDB_OFFICIAL_EXPORT_PATH to a downloaded file."
            )

        candidates = _ordered_export_candidates(
            root,
            dirs,
            new_after=started - 2.0,
        )
        fresh = []
        for p in candidates:
            try:
                key = str(p.resolve())
            except OSError:
                continue
            if key not in seen:
                seen.add(key)
                fresh.append(p)

        if fresh:
            chosen, raw, info = _try_export_candidates(
                fresh, start_year, end_year, audit_rows
            )
            if chosen is not None:
                return raw, {
                    "retrieval_mode": "OFFICIAL_BROWSER_EXPORT_NEW",
                    "source_input": str(chosen),
                    "parse_variant": info.get("variant", ""),
                    "parse_layout": info.get("layout", ""),
                }

        if now - last_notice >= 30:
            remaining = max(0, int(wait_seconds - (now - started)))
            print(f"  Waiting for AfDB browser download... ({remaining}s remaining)")
            last_notice = now

        time.sleep(2)


# =====================================================================
# 6B. FALLBACK: NORMAL CHROME/EDGE SESSION + SAME-ORIGIN SDMX FETCH
#     Cloudflare challenge itself is never automated. If it appears, the user
#     completes it manually in the normal browser. After verification, Python
#     fetches the exact SDMX URL inside that verified browser session.
# =====================================================================
def _is_challenge_html(text: str) -> bool:
    t = (text or "").lower()
    return any(x in t for x in (
        "just a moment",
        "challenges.cloudflare.com",
        "verifying you are human",
        "verify you are human",
        "checking your browser",
        "cf-chl-",
    ))


def _find_chrome_exe() -> Path:
    candidates = [
        os.environ.get("AFDB_CHROME_EXE", ""),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    raise RuntimeError(
        "Could not locate Google Chrome or Microsoft Edge. "
        "Set AFDB_CHROME_EXE to chrome.exe or msedge.exe."
    )


def _port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_websocket_client():
    try:
        import websocket  # type: ignore
        return websocket
    except Exception as e:
        raise RuntimeError(
            "The real-Chrome bridge requires the 'websocket-client' package. "
            "Install it once with:\n"
            "  C:\\WBG\\Python313\\python.exe -m pip install websocket-client\n"
            f"Import error: {e}"
        ) from e


def _launch_real_chrome(root: Path, port: int):
    chrome = _find_chrome_exe()
    profile = root / "afdb_real_chrome_profile"
    profile.mkdir(parents=True, exist_ok=True)

    if _port_open("127.0.0.1", port):
        print(f"  Reusing existing normal-browser session on debug port {port}.")
        return None

    args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        DATASET_PAGE,
    ]

    print(f"  Launching NORMAL browser: {chrome}")
    print("  Persistent profile: afdb_real_chrome_profile")
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _get_debug_targets(port: int):
    r = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=3)
    r.raise_for_status()
    return r.json()


def _wait_for_afdb_target(port: int, timeout: int = 60):
    started = time.time()
    while time.time() - started < timeout:
        try:
            for target in _get_debug_targets(port):
                if target.get("type") != "page":
                    continue
                if "dataportal.opendataforafrica.org" in target.get("url", ""):
                    return target
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(
        f"Could not attach to the normal Chrome/Edge AfDB page on port {port}."
    )


class _CDP:
    def __init__(self, ws_url: str):
        websocket = _ensure_websocket_client()
        self.ws = websocket.create_connection(
            ws_url,
            timeout=30,
            suppress_origin=True,
        )
        self.next_id = 1

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def call(self, method: str, params=None, timeout=60):
        call_id = self.next_id
        self.next_id += 1
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({
            "id": call_id,
            "method": method,
            "params": params or {},
        }))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") != call_id:
                continue
            if "error" in msg:
                raise RuntimeError(f"Chrome DevTools {method} failed: {msg['error']}")
            return msg.get("result", {})

    def evaluate(self, expression: str, await_promise=False, timeout=120):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"Browser JavaScript error: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")


def _challenge_state(cdp: _CDP):
    js = """(() => ({
      title: document.title || "",
      url: location.href || "",
      text: (document.body && document.body.innerText || "").slice(0, 1200)
    }))()"""
    return cdp.evaluate(js) or {}


def _wait_for_human_challenge(cdp: _CDP, wait_seconds: int):
    started = time.time()
    notified = False
    while True:
        state = _challenge_state(cdp)
        title = str(state.get("title", ""))
        body = str(state.get("text", ""))
        url = str(state.get("url", ""))
        challenge = _is_challenge_html(title + "\n" + body)
        if not challenge and "dataportal.opendataforafrica.org" in url:
            return state
        if not notified:
            print("\n  Cloudflare/browser verification is visible in NORMAL Chrome/Edge.")
            print("  Complete it manually in that browser window.")
            print("  The collector will continue automatically after verification.")
            notified = True
        if time.time() - started > wait_seconds:
            raise RuntimeError(
                f"Timed out after {wait_seconds}s waiting for browser verification."
            )
        time.sleep(2)


def _fetch_sdmx_inside_browser(cdp: _CDP, absolute_url: str):
    expr = f"""(async () => {{
      try {{
        const r = await fetch({json.dumps(absolute_url)}, {{
          method: "GET",
          credentials: "include",
          headers: {{"Accept": "application/xml,text/xml,*/*"}}
        }});
        const text = await r.text();
        return {{
          ok: r.ok,
          status: r.status,
          contentType: r.headers.get("content-type") || "",
          challenge: /Just a moment|challenges\\.cloudflare\\.com|verifying you are human|cf-chl-/i.test(text),
          text: text
        }};
      }} catch (e) {{
        return {{ok:false, status:0, contentType:"", challenge:false, text:String(e)}};
      }}
    }})()"""
    return cdp.evaluate(expr, await_promise=True, timeout=180)


def _navigate_browser(cdp: _CDP, url: str):
    cdp.evaluate(
        f"(() => {{ location.href = {json.dumps(url)}; return true; }})()"
    )


def _real_chrome_sdmx_bridge(root, start_year, end_year, data_dir, meta_dir):
    """
    Fetch the 9 LOCKED indicators one at a time inside the verified normal
    Chrome/Edge session.

    This is the decisive V6 change: because each response corresponds to one
    requested numeric SDMX member, returned observations can be assigned to
    that LOCKED code even when the XML returns a public mnemonic instead of the
    numeric request member.
    """
    port = int(os.environ.get("AFDB_CHROME_DEBUG_PORT", "9222"))
    wait_seconds = int(os.environ.get("AFDB_CHROME_WAIT_SECONDS", "600"))

    _launch_real_chrome(root, port)
    target = _wait_for_afdb_target(port, timeout=90)
    cdp = _CDP(target["webSocketDebuggerUrl"])

    ordered_codes = [
        code for code in EXPECTED_LOCKED.keys()
    ]

    frames = []
    audit = []
    urls = []

    try:
        _wait_for_human_challenge(cdp, wait_seconds)
        print(
            "  Normal browser verified. Fetching 9 AfDB indicators "
            "one-by-one in that session..."
        )

        for idx, indicator_code in enumerate(ordered_codes, 1):
            absolute_url = _sdmx_url_for_indicator(
                indicator_code, start_year, end_year
            )
            urls.append(absolute_url)

            print(
                f"    Browser SDMX [{idx}/9] {indicator_code} "
                f"({EXPECTED_LOCKED[indicator_code]['indicator_original']})"
            )

            result = _fetch_sdmx_inside_browser(cdp, absolute_url)

            if not isinstance(result, dict):
                raise RuntimeError(
                    f"Browser bridge returned unexpected result for {indicator_code}."
                )

            # If this exact API path gets a challenge, navigate the normal
            # browser there, let the user solve it, then return to the dataset
            # page and retry.
            if result.get("challenge") or int(result.get("status", 0) or 0) == 403:
                print(
                    f"      {indicator_code}: API path requested browser verification."
                )
                print("      Opening it in the same NORMAL browser...")
                _navigate_browser(cdp, absolute_url)
                time.sleep(2)
                _wait_for_human_challenge(cdp, wait_seconds)

                try:
                    cdp.close()
                except Exception:
                    pass

                target = _wait_for_afdb_target(port, timeout=60)
                cdp = _CDP(target["webSocketDebuggerUrl"])

                result = _fetch_sdmx_inside_browser(cdp, absolute_url)

            if not isinstance(result, dict):
                raise RuntimeError(
                    f"Browser bridge retry returned unexpected result for {indicator_code}."
                )

            status = int(result.get("status", 0) or 0)
            body = str(result.get("text", ""))
            ctype = str(result.get("contentType", ""))

            if not result.get("ok") or status != 200:
                raise RuntimeError(
                    f"{indicator_code}: browser-session SDMX HTTP {status}; "
                    f"content-type={ctype}; response={body[:700]}"
                )

            if result.get("challenge") or _is_challenge_html(body):
                raise RuntimeError(
                    f"{indicator_code}: browser-session response is still "
                    "a Cloudflare challenge."
                )

            content = body.encode("utf-8")

            xml_path = (
                data_dir
                / f"afdb_nbyenxf_sdmx_{indicator_code}_browser.xml"
            )
            xml_path.write_bytes(content)

            raw_i = _parse_sdmx_xml(
                content,
                diagnostic_path=(
                    meta_dir
                    / f"sdmx_diagnostics_{indicator_code}_browser.json"
                ),
                forced_indicator_code=indicator_code,
            )

            raw_i = raw_i[
                raw_i["year"].ge(start_year)
                & raw_i["year"].le(end_year)
                & raw_i["OBS_VALUE"].notna()
            ].copy()

            if raw_i.empty:
                raise RuntimeError(
                    f"{indicator_code}: browser-session SDMX produced zero usable rows."
                )

            frames.append(raw_i)

            response_codes = []
            if "indicator_response_code" in raw_i.columns:
                response_codes = sorted(
                    x
                    for x in raw_i["indicator_response_code"]
                    .dropna()
                    .astype(str)
                    .unique()
                    if x.strip()
                )

            audit.append({
                "indicator_code": indicator_code,
                "indicator_name": EXPECTED_LOCKED[indicator_code]["indicator_original"],
                "response_indicator_values": " | ".join(response_codes[:10]),
                "rows": len(raw_i),
                "countries": raw_i["country_original"].nunique(),
                "min_year": int(raw_i["year"].min()),
                "max_year": int(raw_i["year"].max()),
                "http_status": status,
                "response_bytes": len(content),
                "url": absolute_url,
            })

        (meta_dir / "sdmx_request_urls_per_indicator.txt").write_text(
            "\n".join(urls),
            encoding="utf-8",
        )

        audit_df = pd.DataFrame(audit)
        audit_df.to_csv(
            meta_dir / "sdmx_per_indicator_browser_audit.csv",
            index=False,
            encoding="utf-8-sig",
        )

        raw = pd.concat(frames, ignore_index=True, sort=False)

        missing = EXPECTED_CODES - set(raw["indicator_code"].astype(str))
        if missing:
            raise RuntimeError(
                f"Browser per-indicator SDMX is missing LOCKED indicators: "
                f"{sorted(missing)}"
            )

        return raw, {
            "retrieval_mode": "REAL_CHROME_SDMX_PER_INDICATOR",
            "source_input": str(
                meta_dir / "sdmx_request_urls_per_indicator.txt"
            ),
            "http_status": 200,
            "response_bytes": int(audit_df["response_bytes"].sum()),
            "raw_xml_file": str(data_dir),
        }

    finally:
        try:
            cdp.close()
        except Exception:
            pass


# =====================================================================
# 7. FINAL QC + OUTPUT
# =====================================================================
def _finalize_raw(raw: pd.DataFrame, start_year: int, end_year: int):
    raw = raw.copy()

    raw["indicator_code"] = raw["indicator_code"].astype(str).map(_clean_code)
    raw["year"] = pd.to_numeric(raw["year"], errors="coerce")
    raw["OBS_VALUE"] = pd.to_numeric(raw["OBS_VALUE"], errors="coerce")

    raw = raw[
        raw["indicator_code"].isin(EXPECTED_CODES)
        & raw["year"].ge(start_year)
        & raw["year"].le(end_year)
        & raw["OBS_VALUE"].notna()
        & raw["country_original"].astype(str).str.strip().ne("")
    ].copy()

    # The request intentionally mirrors the UI's 84 country/aggregate members.
    # Production output keeps only sovereign African economies.
    raw = raw[
        raw["country_original"].astype(str).isin(OUTPUT_SOVEREIGN_CODES)
    ].copy()

    if raw.empty:
        raise RuntimeError(
            "AfDB acquisition returned data, but none of the parsed rows mapped "
            "to the 54 sovereign African economies."
        )

    observed_codes = set(raw["indicator_code"].astype(str))
    missing_codes = EXPECTED_CODES - observed_codes
    if missing_codes:
        raise RuntimeError(
            f"AfDB acquisition missing LOCKED indicators: {sorted(missing_codes)}"
        )

    if "dataset_id" not in raw:
        raw["dataset_id"] = DATASET_ID
    if "country_source_code" not in raw:
        raw["country_source_code"] = raw["country_original"].astype(str)
    if "indicator_name" not in raw:
        raw["indicator_name"] = raw["indicator_code"].map(
            lambda c: EXPECTED_LOCKED[str(c)]["indicator_original"]
        )
    if "TIME_PERIOD" not in raw:
        raw["TIME_PERIOD"] = raw["year"].astype("Int64").astype(str)
    if "unit_original" not in raw:
        raw["unit_original"] = ""
    if "scale_value" not in raw:
        raw["scale_value"] = 1.0
    if "frequency_original" not in raw:
        raw["frequency_original"] = "A"

    raw = raw.drop_duplicates(
        ["country_original", "indicator_code", "year", "OBS_VALUE"]
    )

    conflicts = (
        raw.groupby(
            ["country_original", "indicator_code", "year"]
        )["OBS_VALUE"]
        .nunique(dropna=True)
    )
    conflicts = conflicts[conflicts > 1]

    if len(conflicts):
        raise RuntimeError(
            f"AfDB returned {len(conflicts)} conflicting country-indicator-year cells."
        )

    raw = raw.drop_duplicates(
        ["country_original", "indicator_code", "year"],
        keep="last",
    )

    return raw.sort_values(
        ["indicator_code", "country_original", "year"]
    ).reset_index(drop=True)

def main():
    root = _project_root()
    start_year = int(os.environ.get("AFDB_START_YEAR", "2007"))
    end_year = int(os.environ.get("AFDB_END_YEAR", str(datetime.now().year)))

    print(f"  AFDB_COLLECTOR_VERSION: {COLLECTOR_VERSION}")
    print("  AfDB objective: 9 exact LOCKED indicators, 2007-latest")
    print("  V6 retrieval: ONE LOCKED indicator per SDMX request (9 requests total)")
    print("  Acquisition priority:")
    print("    1) direct SDMX URL exposed by AfDB Data Finder UI")
    print("    2) NORMAL Chrome/Edge session + same-origin SDMX fetch after human verification")
    print("  Cloudflare challenge policy: NEVER automated or bypassed")
    print("  AfDB metadata/Count/TimeseriesKey discovery: DISABLED")
    print("  AfDB Knoema mirror: DISABLED")
    print("  AfDB mapping policy: exact numeric source codes only; no fuzzy remapping")
    print("  AfDB SDMX request members: 84 country/aggregate members (exact UI-generated structure)")
    print("  Final output filter: 54 sovereign African economies")
    print(f"  Download window: {start_year}-{end_year}")

    locked = _load_locked_registry(root)

    base = root / f"afdb_fiscal_data_latest_{run_stamp()}"
    data_dir = base / "data"
    meta_dir = base / "metadata"
    log_dir = base / "logs"
    for d in (data_dir, meta_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    locked.to_csv(
        meta_dir / "afdb_locked_registry_used.csv",
        index=False,
        encoding="utf-8-sig",
    )

    pd.DataFrame([
        {
            "country_member_key": key,
            "country_code": COUNTRY_MEMBER_MAP[key],
        }
        for key in COUNTRY_MEMBER_KEYS
    ]).to_csv(
        meta_dir / "afdb_country_member_map_used.csv",
        index=False,
        encoding="utf-8-sig",
    )

    acquisition_audit = []
    raw = None
    meta = None

    # --------------------------------------------------------------
    # PRIMARY: exact SDMX route from the UI
    # --------------------------------------------------------------
    print(
        f"  Trying AfDB direct SDMX: requested members={len(_ALL_COUNTRY_MEMBER_KEYS)}, "
        f"indicators={len(EXPECTED_CODES)} one-by-one"
    )
    try:
        raw, meta = _try_sdmx_direct(
            start_year, end_year, data_dir, meta_dir
        )
        acquisition_audit.append({
            "attempt": "SDMX_DIRECT_PER_INDICATOR",
            "status": "SUCCESS",
            "message": "",
            **meta,
        })
        print("  AfDB direct SDMX SUCCESS")

    except Exception as e:
        acquisition_audit.append({
            "attempt": "SDMX_DIRECT_PER_INDICATOR",
            "status": "FAILED",
            "message": str(e)[:2000],
        })
        print(f"  AfDB direct SDMX unavailable -> {e}")

        # ----------------------------------------------------------
        # FALLBACK: normal real Chrome/Edge session. The human solves
        # Cloudflare verification if shown; only post-verification data
        # retrieval is automated.
        # ----------------------------------------------------------
        print("\n  Switching to NORMAL Chrome/Edge browser-session bridge.")
        print("  Only Cloudflare verification, if shown, is manual.")
        raw, meta = _real_chrome_sdmx_bridge(
            root, start_year, end_year, data_dir, meta_dir
        )

        acquisition_audit.append({
            "attempt": "REAL_CHROME_SDMX_PER_INDICATOR",
            "status": "SUCCESS",
            "message": "",
            **meta,
        })
        print("  AfDB real Chrome/Edge SDMX session SUCCESS")

    raw = _finalize_raw(raw, start_year, end_year)

    qc = validate_observations(
        raw,
        "AFDB_AIH",
        DATASET_ID,
        "country_original",
        "year",
        "OBS_VALUE",
    )

    stamp = datetime.now().isoformat(timespec="seconds")
    raw["DOWNLOAD_TIMESTAMP"] = stamp
    raw["RETRIEVAL_MODE"] = meta["retrieval_mode"]

    raw_file = data_dir / "afdb_fiscal_raw_all_datasets.csv"
    raw.to_csv(raw_file, index=False, encoding="utf-8-sig")

    std = standardize(
        raw,
        "AFDB_AIH",
        country_col="country_original",
        year_col="year",
        value_col="OBS_VALUE",
        indicator_col="indicator_name",
        unit_col="unit_original",
        currency_col=None,
    )
    std["DOWNLOAD_TIMESTAMP"] = stamp
    std["RETRIEVAL_MODE"] = meta["retrieval_mode"]

    std_file = data_dir / "afdb_fiscal_standardized_all_datasets.csv"
    std.to_csv(std_file, index=False, encoding="utf-8-sig")

    coverage = (
        raw.groupby(
            ["dataset_id", "indicator_code", "indicator_name"],
            dropna=False,
        )
        .agg(
            rows=("OBS_VALUE", "size"),
            countries=("country_original", "nunique"),
            min_year=("year", "min"),
            max_year=("year", "max"),
        )
        .reset_index()
        .sort_values("indicator_code")
    )
    coverage["wbfd_mnemonic"] = coverage["indicator_code"].map(
        lambda c: EXPECTED_LOCKED[str(c)]["wbfd_mnemonic"]
    )
    coverage.to_csv(
        meta_dir / "coverage_by_dataset_indicator_unit.csv",
        index=False,
        encoding="utf-8-sig",
    )

    locked.to_csv(
        meta_dir / "afdb_to_fiscal_mapping_best.csv",
        index=False,
        encoding="utf-8-sig",
    )

    pd.DataFrame(acquisition_audit).to_csv(
        log_dir / "afdb_acquisition_audit.csv",
        index=False,
        encoding="utf-8-sig",
    )

    final_summary = pd.DataFrame([{
        "status": "SUCCESS",
        "retrieval_mode": meta["retrieval_mode"],
        "collector_version": COLLECTOR_VERSION,
        "locked_mapping_rows": len(locked),
        "locked_indicators_expected": 9,
        "locked_indicators_observed": raw["indicator_code"].nunique(),
        "downloaded_observations": len(raw),
        "countries": raw["country_original"].nunique(),
        "min_year": int(raw["year"].min()),
        "max_year": int(raw["year"].max()),
        "source_input": meta.get("source_input", ""),
        "raw_file": str(raw_file),
        "standardized_file": str(std_file),
    }])

    final_summary.to_csv(
        log_dir / "download_summary_latest.csv",
        index=False,
        encoding="utf-8-sig",
    )

    print("\n  AfDB FINAL HYBRID refresh SUCCESS")
    print(final_summary.to_string(index=False))
    print(f"\n  AfDB output root: {base.resolve()}")

    return final_summary


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAfDB collector interrupted.")
        sys.exit(130)
