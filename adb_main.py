from __future__ import annotations

import io
import os
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

import pandas as pd

from common import (
    get,
    source_settings,
    filter_years,
    validate_observations,
    standardize,
    run_stamp,
)

SOURCE = "ADB"
API = "https://kidb.adb.org/api"
# ADB's current documented SDMX endpoint is v5.
SDMX = f"{API}/v5/sdmx"

# Registry entries that should not be queried as ordinary observation dataflows.
SKIP_DATA_FLOWS = {"DF_KIDB"}

# Trade dataflows use KIDB_TRADE_DSD instead of the ordinary KIDB_DSD.
TRADE_FLOW_HINTS = ("_ITG", "TRADE")

# ADB documents a maximum of 20 API queries per minute.
# 3.2 seconds keeps discovery safely below that ceiling.
REQUEST_PAUSE_SECONDS = 3.2

COUNTRY_CANDIDATES = [
    "ECONOMY_CODE", "ECONOMY", "REF_AREA", "LOCATION", "COUNTRY_CODE",
    "economy_code", "economy", "Country", "COUNTRY", "country",
]
YEAR_CANDIDATES = ["TIME_PERIOD", "TIME", "year", "Year", "YEAR", "PERIOD"]
VALUE_CANDIDATES = ["OBS_VALUE", "VALUE", "value", "Value"]
INDICATOR_CANDIDATES = ["INDICATOR", "indicator", "INDICATOR_CODE"]
UNIT_CANDIDATES = ["UNIT_MEASURE", "UNIT", "unit", "UNIT_CODE"]

# Expanded Fiscal Survey-oriented discovery vocabulary.
# cfg["fiscal_keywords"] is added on top of this list at runtime.
FISCAL_SEARCH_TERMS = [
    "revenue", "government revenue", "tax", "taxes", "tax revenue",
    "direct tax", "income tax", "personal income", "corporate income",
    "property tax", "goods and services tax", "gst", "value added tax", "vat",
    "excise", "fuel tax", "tobacco tax", "alcohol tax", "sugar tax",
    "international trade tax", "customs", "import dut", "export tax",
    "resource revenue", "resource tax", "commodity revenue", "commodity tax",
    "social contribution", "grant", "interest revenue", "other revenue",
    "expenditure", "government expenditure", "expense",
    "compensation", "employee compensation", "wage", "salary",
    "goods and services", "interest expense", "interest payment",
    "subsid", "social benefit", "social protection",
    "capital expenditure", "capital spending", "investment expenditure",
    "net acquisition", "nonfinancial asset", "non-financial asset",
    "fiscal balance", "overall balance", "primary balance",
    "net lending", "net borrowing", "financing", "external financing",
    "domestic financing", "debt", "gross debt", "public debt",
    "government debt", "external debt", "domestic debt",
]

# Terms that are too broad on their own and create many irrelevant matches.
# They remain useful as part of multi-word phrases above.
WEAK_SINGLE_TERMS = {"tax", "taxes", "debt", "wage", "salary", "grant", "expense"}


def first(df, names):
    return next((c for c in names if c in df.columns), None)


def _clean_text(x):
    if x is None:
        return ""
    if isinstance(x, dict):
        return " ".join(str(v) for v in x.values())
    return str(x)


def get_indicators(flow):
    """Return indicators for one current ADB KIDB registry dataflow."""
    obj = get(f"{API}/dataflow/indicators/{flow}").json()
    if isinstance(obj, dict):
        for k in ("data", "results", "result", "indicators"):
            if isinstance(obj.get(k), list):
                obj = obj[k]
                break
    if not isinstance(obj, list):
        return []

    rows = []
    for x in obj:
        code = x.get("code") or x.get("id") or x.get("value")
        label = x.get("name") or x.get("label") or x.get("text") or ""
        description = x.get("description") or x.get("definition") or x.get("notes") or ""
        unit = x.get("unit") or x.get("unit_measure") or x.get("unitMeasure") or ""
        if code:
            rows.append({
                "flow": str(flow),
                "code": str(code),
                "label": _clean_text(label),
                "description": _clean_text(description),
                "metadata_unit": _clean_text(unit),
            })
    return rows


def discover_all_dataflows():
    """Read the ADB SDMX registry and return every ADB dataflow id."""
    r = get(f"{SDMX}/structure/dataflow/all/all/+")
    root = ET.fromstring(r.content)
    ids = []
    for el in root.iter():
        if el.tag.split("}")[-1].lower() != "dataflow":
            continue
        flow_id = el.attrib.get("id")
        agency = el.attrib.get("agencyID") or el.attrib.get("agency") or ""
        if flow_id and (not agency or agency.upper() == "ADB"):
            ids.append(flow_id)
    return sorted(set(ids))


def fiscal(rows, keywords):
    """
    Find Fiscal Survey-related indicators using code + label + description,
    rather than label alone.
    """
    terms = []
    for k in keywords:
        k = str(k).strip().lower()
        if k and k not in terms:
            terms.append(k)

    out = []
    for x in rows:
        code = x.get("code", "").lower().replace("_", " ")
        label = x.get("label", "").lower()
        desc = x.get("description", "").lower()
        haystack = f"{code} {label} {desc}"

        matched = []
        for term in terms:
            if term in haystack:
                # Guard against some very broad one-word false positives by
                # requiring an explicitly fiscal context in the same metadata.
                if term in WEAK_SINGLE_TERMS:
                    fiscal_context = any(
                        anchor in haystack
                        for anchor in (
                            "government", "fiscal", "public", "revenue", "expenditure",
                            "finance", "financing", "budget", "tax", "debt",
                        )
                    )
                    if not fiscal_context:
                        continue
                matched.append(term)

        if matched:
            y = dict(x)
            y["matched_terms"] = " | ".join(sorted(set(matched)))
            out.append(y)
    return out


def chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _norm(s):
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9%]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _terms(cell):
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return []
    return [_norm(x) for x in str(cell).split("|") if _norm(x)]



# ---------------------------------------------------------------------
# Embedded Fiscal Survey mapping specification
# ---------------------------------------------------------------------
# No external mapping workbook is required at runtime.
# These 64 rules are embedded directly in this collector.
EMBEDDED_MAPPING_RULES = [{'Priority': '1',
  'Fiscal Survey Code': 'GGREVTOTLCN',
  'Fiscal Survey Variable': 'Total revenue',
  'Exact ADB Indicator Code': 'GR_G14_GG_XGDP_RT_PS',
  'Include Any (| separated)': 'government revenue|total revenue',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'tax|grant',
  'Auto Decision': 'EXACT_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Exact known KIDB candidate; preserve institutional-scope/unit checks.'},
 {'Priority': '2',
  'Fiscal Survey Code': 'GGREVTAXTCN',
  'Fiscal Survey Variable': 'Tax revenue (including social contributions)',
  'Exact ADB Indicator Code': 'GRT_G14_GG_XGDP_RT_PS',
  'Include Any (| separated)': 'tax revenue|government taxes',
  'Include All (| separated)': 'government',
  'Exclude Any (| separated)': 'tax on|income tax|property tax|vat|excise|customs',
  'Auto Decision': 'CONDITIONAL',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Only final if ADB definition includes social contributions.'},
 {'Priority': '3',
  'Fiscal Survey Code': 'GGREVDRCTCN',
  'Fiscal Survey Variable': 'Direct taxes',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'direct tax|direct taxes',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'indirect',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only; verify source definition.'},
 {'Priority': '4',
  'Fiscal Survey Code': 'GGREVDRINCN',
  'Fiscal Survey Variable': 'Income tax',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'income tax|taxes on income',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'personal|corporate|company',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Aggregate income-tax candidate.'},
 {'Priority': '5',
  'Fiscal Survey Code': 'GGREVDRPICN',
  'Fiscal Survey Variable': 'Personal income tax',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'personal income tax|individual income tax|personal tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'corporate|company',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only until definition/scope confirmed.'},
 {'Priority': '6',
  'Fiscal Survey Code': 'GGREVDRCICN',
  'Fiscal Survey Variable': 'Corporate income tax',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'corporate income tax|company income tax|corporate tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'resource|commodity|personal',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only until definition/scope confirmed.'},
 {'Priority': '7',
  'Fiscal Survey Code': 'GGREVDRCRCN',
  'Fiscal Survey Variable': 'Corporate tax, resource (commodity) revenues',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'resource corporate tax|commodity corporate tax|resource income tax',
  'Include All (| separated)': 'corporate',
  'Exclude Any (| separated)': 'personal',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Resource-sector corporate tax only.'},
 {'Priority': '8',
  'Fiscal Survey Code': 'GGREVDROICN',
  'Fiscal Survey Variable': 'Other (unallocable income taxes)',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other income tax|unallocable income tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'personal|corporate',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '9',
  'Fiscal Survey Code': 'GGREVDRPRCN',
  'Fiscal Survey Variable': 'Taxes on property',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'property tax|taxes on property',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only until definition confirmed.'},
 {'Priority': '10',
  'Fiscal Survey Code': 'GGREVDROTCN',
  'Fiscal Survey Variable': 'Other direct taxes',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other direct tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'income|property',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '11',
  'Fiscal Survey Code': 'GGREVIDGSCN',
  'Fiscal Survey Variable': 'Taxes on goods and services',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'taxes on goods and services|goods and services tax|tax on goods and services',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'vat|value added|excise',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Aggregate GST-type tax candidate.'},
 {'Priority': '12',
  'Fiscal Survey Code': 'GGREVGNFSCN',
  'Fiscal Survey Variable': 'General taxes on goods & services (incl. VAT/Sales)',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'general taxes on goods and services|general sales tax|sales tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'excise|specific',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Verify whether VAT/sales taxes are included.'},
 {'Priority': '13',
  'Fiscal Survey Code': 'GGREVVATTCN',
  'Fiscal Survey Variable': 'VAT',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'value added tax|value-added tax|vat',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only until source definition confirmed.'},
 {'Priority': '14',
  'Fiscal Survey Code': 'GGREVEXSECN',
  'Fiscal Survey Variable': 'Excise tax',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'excise tax|excise taxes|excise duty|excise duties',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'fuel|tobacco|alcohol|sugar',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Aggregate excise candidate.'},
 {'Priority': '15',
  'Fiscal Survey Code': 'GGREVEXFLCN',
  'Fiscal Survey Variable': 'Excise tax on fuel',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'fuel excise|excise on fuel|fuel tax|petroleum excise',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '16',
  'Fiscal Survey Code': 'GGREVEXTBCN',
  'Fiscal Survey Variable': 'Excise tax on tobacco',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'tobacco excise|excise on tobacco|tobacco tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '17',
  'Fiscal Survey Code': 'GGREVEXALCN',
  'Fiscal Survey Variable': 'Excise tax on alcohol',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'alcohol excise|excise on alcohol|alcohol tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '18',
  'Fiscal Survey Code': 'GGREVEXSGCN',
  'Fiscal Survey Variable': 'Excise tax on sugar-sweetened beverages',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'sugar sweetened beverage tax|sugar-sweetened beverage tax|ssb tax|sugar tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '19',
  'Fiscal Survey Code': 'GGREVEXOTCN',
  'Fiscal Survey Variable': 'Other excise taxes',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other excise tax|other excise duties',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'fuel|tobacco|alcohol|sugar',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '20',
  'Fiscal Survey Code': 'GGREVGSOTCN',
  'Fiscal Survey Variable': 'Other taxes on goods and services',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other taxes on goods and services|other goods and services taxes',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '21',
  'Fiscal Survey Code': 'GGREVTRDECN',
  'Fiscal Survey Variable': 'Taxes on international trade and transactions',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'international trade tax|taxes on international trade|trade taxes|trade and transactions tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'customs|import|export',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Aggregate trade-tax candidate.'},
 {'Priority': '22',
  'Fiscal Survey Code': 'GGREVCUSTCN',
  'Fiscal Survey Variable': 'Customs & other import duties',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'customs duty|customs duties|import duty|import duties|customs and import',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'export',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '23',
  'Fiscal Survey Code': 'GGREVEXPTCN',
  'Fiscal Survey Variable': 'Taxes on exports',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'export tax|export taxes|export duty|export duties',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'import',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '24',
  'Fiscal Survey Code': 'GGREVTROTCN',
  'Fiscal Survey Variable': 'Other international trade taxes',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other international trade tax|other trade tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'customs|import|export',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '25',
  'Fiscal Survey Code': 'GGREVTOTRCN',
  'Fiscal Survey Variable': 'Other resource (commodity) sector taxes',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'resource tax|commodity tax|natural resource tax',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'corporate',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '26',
  'Fiscal Survey Code': 'GGREVSSOCCN',
  'Fiscal Survey Variable': 'Social contributions',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'social contribution|social contributions|social security contribution',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'benefit',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '27',
  'Fiscal Survey Code': 'GGREVTOTHCN',
  'Fiscal Survey Variable': 'Other taxes',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other taxes|miscellaneous taxes',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'direct|income|property|goods|excise|trade|resource',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'LOW',
  'Rule Note': 'Broad category; review required.'},
 {'Priority': '28',
  'Fiscal Survey Code': 'GGREVCOMMCN',
  'Fiscal Survey Variable': 'Non-tax resource (commodity) revenues',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'non-tax resource revenue|nontax resource revenue|commodity revenue|resource revenue',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'tax',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Must be non-tax resource revenue.'},
 {'Priority': '29',
  'Fiscal Survey Code': 'GGREVGRNTCN',
  'Fiscal Survey Variable': 'Grants revenue',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'grants revenue|grant revenue|government grants received|grants received',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'capital grant expenditure',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Revenue-side grants.'},
 {'Priority': '30',
  'Fiscal Survey Code': 'GGREVOTHRCN',
  'Fiscal Survey Variable': 'Other revenue',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other revenue|miscellaneous revenue',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'tax|grant|interest|resource',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'LOW',
  'Rule Note': 'Broad category; review required.'},
 {'Priority': '31',
  'Fiscal Survey Code': 'GGREVINTRCN',
  'Fiscal Survey Variable': 'Interest revenue',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'interest revenue|interest receipts|interest income',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'expense|payment',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Revenue-side interest.'},
 {'Priority': '32',
  'Fiscal Survey Code': 'GGEXPTOTLCN',
  'Fiscal Survey Variable': 'Total expenditure',
  'Exact ADB Indicator Code': 'GX_G14_GG_XGDP_RT_PS',
  'Include Any (| separated)': 'government expenditure|total expenditure',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'net lending',
  'Auto Decision': 'EXACT_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Exact known KIDB candidate; preserve scope/unit checks.'},
 {'Priority': '33',
  'Fiscal Survey Code': 'GGEXPCRNTCN',
  'Fiscal Survey Variable': 'Current expenditure',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'current expenditure|current expense',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'capital',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '34',
  'Fiscal Survey Code': 'GGEXPWAGECN',
  'Fiscal Survey Variable': 'Wages and compensation',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'compensation of employees|employee compensation|wages and salaries|wages|salary',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '35',
  'Fiscal Survey Code': 'GGEXPGNFSCN',
  'Fiscal Survey Variable': 'Use of goods and services',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'use of goods and services|goods and services expense|government consumption goods and services',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'tax',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Economic expenditure category.'},
 {'Priority': '36',
  'Fiscal Survey Code': 'GGEXPINTPCN',
  'Fiscal Survey Variable': 'Interest expense',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'interest expense|interest expenditure|interest payment|interest payments',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'revenue|receipt',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Aggregate interest expense.'},
 {'Priority': '37',
  'Fiscal Survey Code': 'GGEXPINTECN',
  'Fiscal Survey Variable': 'Interest payments on external public debt',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'external debt interest|interest on external debt|external interest payment',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'domestic',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'External public debt interest.'},
 {'Priority': '38',
  'Fiscal Survey Code': 'GGEXPINTDCN',
  'Fiscal Survey Variable': 'Interest payments on domestic public debt',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'domestic debt interest|interest on domestic debt|domestic interest payment',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'external',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Domestic public debt interest.'},
 {'Priority': '39',
  'Fiscal Survey Code': 'GGEXPTRNSCN',
  'Fiscal Survey Variable': 'Current transfers',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'current transfers|current transfer',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'capital',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '40',
  'Fiscal Survey Code': 'GGEXPTPNSCN',
  'Fiscal Survey Variable': 'Social security benefits',
  'Exact ADB Indicator Code': 'GEFTE_SP_XGDP_RT_PS',
  'Include Any (| separated)': 'social security benefit|social benefits|social protection',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'assistance',
  'Auto Decision': 'PARTIAL_REVIEW',
  'Confidence': 'LOW',
  'Rule Note': 'Functional social protection is not automatically equivalent to social-security benefits.'},
 {'Priority': '41',
  'Fiscal Survey Code': 'GGEXPTSOCCN',
  'Fiscal Survey Variable': 'Social assistance',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'social assistance|social assistance benefits',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'security contribution',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '42',
  'Fiscal Survey Code': 'GGEXPTOTSCN',
  'Fiscal Survey Variable': 'Employment-related social benefits',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'employment related social benefit|employment-related social benefit',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '43',
  'Fiscal Survey Code': 'GGEXPSUBSCN',
  'Fiscal Survey Variable': 'Subsidies to businesses',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'subsidies to business|business subsidies|subsidies|subsidy',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'social',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Verify recipient/business scope.'},
 {'Priority': '44',
  'Fiscal Survey Code': 'GGEXPTOTHCN',
  'Fiscal Survey Variable': 'All other current transfers',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other current transfers|all other current transfers',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '45',
  'Fiscal Survey Code': 'GGEXPCROTCN',
  'Fiscal Survey Variable': 'Other current expenditure',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other current expenditure|other current expense',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'transfer',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '46',
  'Fiscal Survey Code': 'GGEXPCAPTCN',
  'Fiscal Survey Variable': 'Capital expenditure',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'capital expenditure|capital spending|development expenditure',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'current',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '47',
  'Fiscal Survey Code': 'GGEXPKINVCN',
  'Fiscal Survey Variable': 'Capital investment',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'capital investment|government investment|public investment|gross fixed capital formation',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'private',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Verify whether definition matches Fiscal Survey capital investment.'},
 {'Priority': '48',
  'Fiscal Survey Code': 'GGEXPKCFKCN',
  'Fiscal Survey Variable': 'Consumption of fixed capital',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'consumption of fixed capital|depreciation',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Candidate only.'},
 {'Priority': '49',
  'Fiscal Survey Code': 'GGEXPKTRNCN',
  'Fiscal Survey Variable': 'Other capital expenditure',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other capital expenditure|capital transfers|other capital spending',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '50',
  'Fiscal Survey Code': 'GGEXPOTHRCN',
  'Fiscal Survey Variable': 'Other expenditure',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other expenditure|miscellaneous expenditure',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'current|capital',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'LOW',
  'Rule Note': 'Broad category; review required.'},
 {'Priority': '51',
  'Fiscal Survey Code': 'GGBALOVRACN',
  'Fiscal Survey Variable': 'Overall fiscal balance (accrual / above-the-line)',
  'Exact ADB Indicator Code': 'GXCNL_G14_GG_XGDP_RT_PS',
  'Include Any (| separated)': 'net lending net borrowing|net lending/net borrowing|overall fiscal balance|fiscal balance',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'cash',
  'Auto Decision': 'EXACT_CANDIDATE_BASIS_CHECK',
  'Confidence': 'HIGH',
  'Rule Note': 'Known closest concept; verify accounting basis and institutional coverage.'},
 {'Priority': '52',
  'Fiscal Survey Code': 'GGBALOVRLCN',
  'Fiscal Survey Variable': 'Overall fiscal balance (cash / below-the-line)',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'cash balance|cash fiscal balance|overall balance cash',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'accrual|net lending',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Do not use generic net lending/borrowing as cash balance.'},
 {'Priority': '53',
  'Fiscal Survey Code': 'GGBALDISCCN',
  'Fiscal Survey Variable': 'Discrepancy between cash and commitment balance',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'cash commitment discrepancy|statistical discrepancy|fiscal discrepancy',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': '',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Review definition carefully.'},
 {'Priority': '54',
  'Fiscal Survey Code': 'GGFINEXTLCN',
  'Fiscal Survey Variable': 'Net external financing',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'net external financing|external financing net',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'domestic',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Financing flow, not debt stock.'},
 {'Priority': '55',
  'Fiscal Survey Code': 'GGFINEDSBCN',
  'Fiscal Survey Variable': 'External debt disbursements',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'external debt disbursement|external disbursement|foreign loan disbursement',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'domestic',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Flow variable.'},
 {'Priority': '56',
  'Fiscal Survey Code': 'GGFINEAMTCN',
  'Fiscal Survey Variable': 'Amortization of external debt',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'external debt amortization|external amortization|external debt repayment',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'domestic|interest',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Principal repayment flow.'},
 {'Priority': '57',
  'Fiscal Survey Code': 'GGFINEOTHCN',
  'Fiscal Survey Variable': 'Other external financing',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other external financing',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'domestic',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '58',
  'Fiscal Survey Code': 'GGFINDOMTCN',
  'Fiscal Survey Variable': 'Net domestic financing',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'net domestic financing|domestic financing net',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'external',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Financing flow.'},
 {'Priority': '59',
  'Fiscal Survey Code': 'GGFINDDSBCN',
  'Fiscal Survey Variable': 'Domestic debt disbursements',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'domestic debt disbursement|domestic borrowing disbursement',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'external',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Flow variable.'},
 {'Priority': '60',
  'Fiscal Survey Code': 'GGFINDAMTCN',
  'Fiscal Survey Variable': 'Amortization of domestic debt',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'domestic debt amortization|domestic amortization|domestic debt repayment',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'external|interest',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Principal repayment flow.'},
 {'Priority': '61',
  'Fiscal Survey Code': 'GGDBTDOOTCN',
  'Fiscal Survey Variable': 'Other domestic financing',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'other domestic financing',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'external',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'MEDIUM',
  'Rule Note': 'Candidate only.'},
 {'Priority': '62',
  'Fiscal Survey Code': 'GGDBTTOTLCN',
  'Fiscal Survey Variable': 'Total gross debt',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'total gross debt|gross public debt|general government gross debt|government gross debt|public debt',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'external debt|domestic debt|private',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Debt stock; verify general-government/public-sector scope.'},
 {'Priority': '63',
  'Fiscal Survey Code': 'GGDBTEXTLCN',
  'Fiscal Survey Variable': 'External gross debt',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'external gross debt|government external debt|public external debt|external public debt',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'private|domestic',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Debt stock; verify government scope.'},
 {'Priority': '64',
  'Fiscal Survey Code': 'GGDBTDOMTCN',
  'Fiscal Survey Variable': 'Domestic gross debt',
  'Exact ADB Indicator Code': '',
  'Include Any (| separated)': 'domestic gross debt|government domestic debt|public domestic debt|domestic public debt',
  'Include All (| separated)': '',
  'Exclude Any (| separated)': 'external|private',
  'Auto Decision': 'AUTO_CANDIDATE',
  'Confidence': 'HIGH',
  'Rule Note': 'Debt stock; verify government scope.'}]

FISCAL_CATEGORY_BY_CODE = {'GGREVTOTLCN': 'Revenue',
 'GGREVTAXTCN': 'Revenue',
 'GGREVDRCTCN': 'Revenue',
 'GGREVDRINCN': 'Revenue',
 'GGREVDRPICN': 'Revenue',
 'GGREVDRCICN': 'Revenue',
 'GGREVDRCRCN': 'Revenue',
 'GGREVDROICN': 'Revenue',
 'GGREVDRPRCN': 'Revenue',
 'GGREVDROTCN': 'Revenue',
 'GGREVIDGSCN': 'Revenue',
 'GGREVGNFSCN': 'Revenue',
 'GGREVVATTCN': 'Revenue',
 'GGREVEXSECN': 'Revenue',
 'GGREVEXFLCN': 'Revenue',
 'GGREVEXTBCN': 'Revenue',
 'GGREVEXALCN': 'Revenue',
 'GGREVEXSGCN': 'Revenue',
 'GGREVEXOTCN': 'Revenue',
 'GGREVGSOTCN': 'Revenue',
 'GGREVTRDECN': 'Revenue',
 'GGREVCUSTCN': 'Revenue',
 'GGREVEXPTCN': 'Revenue',
 'GGREVTROTCN': 'Revenue',
 'GGREVTOTRCN': 'Revenue',
 'GGREVSSOCCN': 'Revenue',
 'GGREVTOTHCN': 'Revenue',
 'GGREVCOMMCN': 'Revenue',
 'GGREVGRNTCN': 'Revenue',
 'GGREVOTHRCN': 'Revenue',
 'GGREVINTRCN': 'Revenue',
 'GGEXPTOTLCN': 'Expenditure',
 'GGEXPCRNTCN': 'Expenditure',
 'GGEXPWAGECN': 'Expenditure',
 'GGEXPGNFSCN': 'Expenditure',
 'GGEXPINTPCN': 'Expenditure',
 'GGEXPINTECN': 'Expenditure',
 'GGEXPINTDCN': 'Expenditure',
 'GGEXPTRNSCN': 'Expenditure',
 'GGEXPTPNSCN': 'Expenditure',
 'GGEXPTSOCCN': 'Expenditure',
 'GGEXPTOTSCN': 'Expenditure',
 'GGEXPSUBSCN': 'Expenditure',
 'GGEXPTOTHCN': 'Expenditure',
 'GGEXPCROTCN': 'Expenditure',
 'GGEXPCAPTCN': 'Expenditure',
 'GGEXPKINVCN': 'Expenditure',
 'GGEXPKCFKCN': 'Expenditure',
 'GGEXPKTRNCN': 'Expenditure',
 'GGEXPOTHRCN': 'Expenditure',
 'GGBALOVRACN': 'Balance',
 'GGBALOVRLCN': 'Balance',
 'GGBALDISCCN': 'Balance',
 'GGFINEXTLCN': 'Financing',
 'GGFINEDSBCN': 'Financing',
 'GGFINEAMTCN': 'Financing',
 'GGFINEOTHCN': 'Financing',
 'GGFINDOMTCN': 'Financing',
 'GGFINDDSBCN': 'Financing',
 'GGFINDAMTCN': 'Financing',
 'GGDBTDOOTCN': 'Financing',
 'GGDBTTOTLCN': 'Debt',
 'GGDBTEXTLCN': 'Debt',
 'GGDBTDOMTCN': 'Debt'}

def load_mapping_rules():
    """Return the embedded 64-variable Fiscal Survey mapping rules."""
    rules = pd.DataFrame(EMBEDDED_MAPPING_RULES).copy()
    required = [
        "Priority", "Fiscal Survey Code", "Fiscal Survey Variable",
        "Exact ADB Indicator Code", "Include Any (| separated)",
        "Include All (| separated)", "Exclude Any (| separated)",
        "Auto Decision", "Confidence", "Rule Note",
    ]
    missing = [c for c in required if c not in rules.columns]
    if missing:
        raise ValueError(f"Embedded mapping rules are missing columns: {missing}")
    rules["Priority"] = pd.to_numeric(
        rules["Priority"], errors="coerce"
    ).fillna(9999).astype(int)
    return rules


def _match_rule(ind, rule):
    code = str(ind.get("code", "")).strip()
    exact = str(rule.get("Exact ADB Indicator Code", "")).strip()
    hay = _norm(" ".join([
        code,
        str(ind.get("label", "")),
        str(ind.get("description", "")),
    ]))

    confidence = str(rule.get("Confidence", "LOW")).upper().strip() or "LOW"
    conf_score = {"HIGH": 300, "MEDIUM": 200, "LOW": 100}.get(confidence, 50)

    # Exact indicator-code mappings always outrank text rules.
    if exact and code.upper() == exact.upper():
        return {
            "match_type": "EXACT_CODE",
            "mapping_score": 10000 + conf_score,
            "matched_phrases": exact,
        }

    include_any = _terms(rule.get("Include Any (| separated)", ""))
    include_all = _terms(rule.get("Include All (| separated)", ""))
    exclude_any = _terms(rule.get("Exclude Any (| separated)", ""))

    if exclude_any and any(t in hay for t in exclude_any):
        return None
    if include_all and not all(t in hay for t in include_all):
        return None
    any_hits = [t for t in include_any if t in hay]
    if include_any and not any_hits:
        return None
    if not include_any and not include_all:
        return None

    all_hits = [t for t in include_all if t in hay]
    specificity = sum(len(t.split()) for t in (any_hits + all_hits))
    return {
        "match_type": "TEXT_RULE",
        "mapping_score": conf_score + specificity,
        "matched_phrases": " | ".join(any_hits + all_hits),
    }


def build_mapping_candidates(fiscal_inventory: pd.DataFrame, rules: pd.DataFrame):
    """Return all candidate mappings plus one best match per flow+indicator code."""
    candidates = []
    for _, ind in fiscal_inventory.iterrows():
        for _, rule in rules.iterrows():
            hit = _match_rule(ind, rule)
            if hit is None:
                continue
            candidates.append({
                "flow": ind.get("flow", ""),
                "adb_indicator_code": ind.get("code", ""),
                "adb_indicator_label": ind.get("label", ""),
                "adb_indicator_description": ind.get("description", ""),
                "fiscal_survey_code": rule["Fiscal Survey Code"],
                "fiscal_survey_variable": rule["Fiscal Survey Variable"],
                "mapping_status": rule["Auto Decision"],
                "mapping_confidence": rule["Confidence"],
                "rule_note": rule["Rule Note"],
                "priority": int(rule["Priority"]),
                **hit,
            })

    cand = pd.DataFrame(candidates)
    if cand.empty:
        cols = [
            "flow", "adb_indicator_code", "adb_indicator_label",
            "adb_indicator_description", "fiscal_survey_code",
            "fiscal_survey_variable", "mapping_status", "mapping_confidence",
            "rule_note", "priority", "match_type", "mapping_score",
            "matched_phrases",
        ]
        cand = pd.DataFrame(columns=cols)
        return cand, cand.copy()

    cand = cand.sort_values(
        ["flow", "adb_indicator_code", "mapping_score", "priority"],
        ascending=[True, True, False, True],
    ).reset_index(drop=True)
    cand["candidate_rank"] = cand.groupby(
        ["flow", "adb_indicator_code"]
    ).cumcount() + 1
    best = cand[cand["candidate_rank"] == 1].copy()
    return cand, best





def export_runtime_mapping_workbook(
    output_path: Path,
    fiscal_inventory: pd.DataFrame,
    mapping_candidates: pd.DataFrame,
    mapping_best: pd.DataFrame,
    rules: pd.DataFrame,
    discovery_log: pd.DataFrame | None = None,
):
    """
    Create the mapping-result workbook FROM SCRATCH.

    No input Excel workbook is used. The workbook is generated entirely from:
      1) embedded Fiscal Survey rules,
      2) indicators discovered during this run, and
      3) runtime mapping results.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if mapping_best is not None and not mapping_best.empty:
        by_fs = (
            mapping_best.sort_values(
                ["fiscal_survey_code", "mapping_score", "priority"],
                ascending=[True, False, True],
            )
            .drop_duplicates(subset=["fiscal_survey_code"], keep="first")
            .copy()
        )
    else:
        by_fs = pd.DataFrame()

    full = rules[[
        "Priority", "Fiscal Survey Code", "Fiscal Survey Variable",
        "Auto Decision", "Confidence", "Rule Note"
    ]].copy()
    full.insert(
        1,
        "Category",
        full["Fiscal Survey Code"].map(FISCAL_CATEGORY_BY_CODE).fillna("")
    )

    if not by_fs.empty:
        picked = by_fs[[
            "fiscal_survey_code", "flow", "adb_indicator_code",
            "adb_indicator_label", "adb_indicator_description",
            "mapping_status", "mapping_confidence", "match_type",
            "mapping_score", "matched_phrases", "rule_note"
        ]].rename(columns={
            "fiscal_survey_code": "Fiscal Survey Code",
            "flow": "ADB Flow",
            "adb_indicator_code": "ADB Indicator Code",
            "adb_indicator_label": "ADB Indicator Label",
            "adb_indicator_description": "ADB Indicator Description",
            "mapping_status": "Runtime Mapping Status",
            "mapping_confidence": "Runtime Confidence",
            "match_type": "Match Type",
            "mapping_score": "Mapping Score",
            "matched_phrases": "Matched Phrases",
            "rule_note": "Runtime Rule Note",
        })
        full = full.merge(picked, on="Fiscal Survey Code", how="left")
    else:
        for c in [
            "ADB Flow", "ADB Indicator Code", "ADB Indicator Label",
            "ADB Indicator Description", "Runtime Mapping Status",
            "Runtime Confidence", "Match Type", "Mapping Score",
            "Matched Phrases", "Runtime Rule Note"
        ]:
            full[c] = ""

    full["Final Runtime Status"] = full["Runtime Mapping Status"].fillna("")
    full.loc[full["Final Runtime Status"].eq(""), "Final Runtime Status"] = "GAP"

    mapped_only = full[full["Final Runtime Status"] != "GAP"].copy()

    discovered = fiscal_inventory.copy()
    if mapping_best is not None and not mapping_best.empty:
        b = mapping_best[[
            "flow", "adb_indicator_code", "fiscal_survey_code",
            "fiscal_survey_variable", "mapping_status",
            "mapping_confidence", "match_type", "mapping_score",
            "matched_phrases"
        ]].copy()
        discovered = discovered.merge(
            b,
            left_on=["flow", "code"],
            right_on=["flow", "adb_indicator_code"],
            how="left",
        )

    if "mapping_status" not in discovered.columns:
        discovered["mapping_status"] = "UNMAPPED"
    else:
        discovered["mapping_status"] = discovered["mapping_status"].fillna("UNMAPPED")

    summary = pd.DataFrame([
        {"Metric": "Fiscal Survey variables", "Value": int(len(rules))},
        {"Metric": "Discovered fiscal ADB indicators", "Value": int(len(fiscal_inventory))},
        {"Metric": "Mapped ADB indicators (best)", "Value": int(len(mapping_best))},
        {
            "Metric": "Mapped Fiscal Survey codes",
            "Value": int(mapped_only["Fiscal Survey Code"].nunique()) if not mapped_only.empty else 0,
        },
        {
            "Metric": "GAP Fiscal Survey codes",
            "Value": int((full["Final Runtime Status"] == "GAP").sum()),
        },
        {"Metric": "Candidate links", "Value": int(len(mapping_candidates))},
    ])

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        full.to_excel(writer, sheet_name="ADB Full Mapping", index=False)
        mapped_only.to_excel(writer, sheet_name="ADB Mapped Only", index=False)
        discovered.to_excel(writer, sheet_name="Discovered ADB Indicators", index=False)
        mapping_candidates.to_excel(writer, sheet_name="Runtime Candidates", index=False)
        rules.to_excel(writer, sheet_name="Embedded Mapping Rules", index=False)
        if discovery_log is not None:
            discovery_log.to_excel(writer, sheet_name="Dataflow Discovery", index=False)

        # Basic readable formatting without needing a template workbook.
        wb = writer.book
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                cell.font = cell.font.copy(bold=True)
            # Keep widths bounded so large descriptions do not make the sheet unusable.
            for col_cells in ws.columns:
                letter = col_cells[0].column_letter
                max_len = 0
                for cell in col_cells[:200]:
                    value = "" if cell.value is None else str(cell.value)
                    max_len = max(max_len, len(value))
                ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 45)

    return {
        "runtime_workbook": str(output_path),
        "discovered_indicators": int(len(fiscal_inventory)),
        "mapped_adb_indicators": int(len(mapping_best)),
        "mapped_fiscal_survey_codes": int(mapped_only["Fiscal Survey Code"].nunique()) if not mapped_only.empty else 0,
        "gap_fiscal_survey_codes": int((full["Final Runtime Status"] == "GAP").sum()),
    }


def _mapping_lookup(best: pd.DataFrame, flow: str):
    if best is None or best.empty:
        return {}
    sub = best[best["flow"].astype(str) == str(flow)]
    return {
        str(r["adb_indicator_code"]): r.to_dict()
        for _, r in sub.iterrows()
    }


def annotate_mapping(frame: pd.DataFrame, indicator_codes, lookup):
    """Add Fiscal Survey mapping metadata to a row-aligned dataframe."""
    codes = [str(x) for x in indicator_codes]
    records = [lookup.get(c, {}) for c in codes]
    frame["FISCAL_SURVEY_CODE"] = [r.get("fiscal_survey_code", "") for r in records]
    frame["FISCAL_SURVEY_VARIABLE"] = [r.get("fiscal_survey_variable", "") for r in records]
    frame["MAPPING_STATUS"] = [r.get("mapping_status", "UNMAPPED") for r in records]
    frame["MAPPING_CONFIDENCE"] = [r.get("mapping_confidence", "") for r in records]
    frame["MAPPING_MATCH_TYPE"] = [r.get("match_type", "") for r in records]
    frame["MAPPING_SCORE"] = [r.get("mapping_score", "") for r in records]
    return frame


def _extract_sdmx_xml_message(text: str) -> str:
    """Best-effort extraction of an SDMX XML error/message."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
        parts = []
        for el in root.iter():
            tag = el.tag.split("}")[-1].lower()
            if tag in {"text", "message", "error", "messagetext"}:
                value = (el.text or "").strip()
                if value:
                    parts.append(value)
        if parts:
            return " | ".join(dict.fromkeys(parts))[:1000]
    except Exception:
        pass
    return re.sub(r"\s+", " ", text[:1000]).strip()


def _sdmx_key(flow, codes):
    """
    Build the SDMX key for ADB v5.

    Main KIDB_DSD:
        FREQUENCY.INDICATOR(S).ECONOMY_CODE(S)
        -> A.<INDICATORS>.

    KIDB_TRADE_DSD:
        FREQUENCY.INDICATOR(S).ECONOMY_CODE(S).COUNTERPART_AREA(S).CURRENCY
        -> A.<INDICATORS>...
    """
    code_part = "+".join(codes)
    upper = str(flow).upper()
    if any(h in upper for h in TRADE_FLOW_HINTS):
        return f"A.{code_part}..."
    return f"A.{code_part}."


ADB_ECONOMY_CODES = [
    "AFG", "ARM", "AUS", "AZE", "BAN", "BHU", "BRU", "CAM", "PRC", "COO",
    "FIJ", "FSM", "GEO", "HKG", "IND", "INO", "JPN", "KAZ", "KGZ", "KIR",
    "KOR", "LAO", "MAL", "MLD", "MON", "MYA", "NAU", "NEP", "NZL", "NIU",
    "PAK", "PHI", "PLW", "PNG", "RMI", "SAM", "SIN", "SOL", "SRI", "TAP",
    "TAJ", "THA", "TIM", "TKM", "TON", "TUR", "TUV", "UZB", "VAN", "VIE",
]
ADB_ECONOMY_CHUNK_SIZE = 10
ADB_DOCUMENTED_MAX_YEAR = 2024
ADB_RETRYABLE_STATUS = {429, 502, 503, 504}


def _sdmx_key_for_economies(flow, codes, economies):
    code_part = "+".join(codes)
    econ_part = "+".join(economies)
    upper = str(flow).upper()
    if any(h in upper for h in TRADE_FLOW_HINTS):
        return f"A.{code_part}.{econ_part}.."
    return f"A.{code_part}.{econ_part}"


def _http_status(exc):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def _parse_sdmx_csv_response(r, flow, codes, key):
    text = r.text or ""
    content_type = (r.headers.get("Content-Type") or "").lower()
    prefix = text.lstrip()[:100].lower()

    if prefix.startswith("<?xml") or prefix.startswith("<message") or "xml" in content_type:
        msg = _extract_sdmx_xml_message(text)
        raise RuntimeError(
            f"ADB returned SDMX XML instead of CSV for flow={flow}, "
            f"codes={len(codes)}, key={key}. Message: {msg}"
        )

    if not text.strip():
        raise RuntimeError(
            f"ADB returned an empty response for flow={flow}, key={key}"
        )

    raw = pd.read_csv(io.StringIO(text), dtype=str, low_memory=False)
    if len(raw.columns) == 1:
        only = str(raw.columns[0]).strip().lower()
        if only.startswith("<?xml") or only.startswith("<!doctype") or only.startswith("<html"):
            raise RuntimeError(
                f"ADB response was not CSV for flow={flow}, key={key}; "
                f"first column={raw.columns[0]!r}"
            )
    return raw


def _request_sdmx(flow, codes, key, start, end=None):
    params = {"startPeriod": start, "format": "sdmx-csv"}
    if end is not None:
        params["endPeriod"] = end
    url = f"{SDMX}/data/ADB,{flow}/{key}"
    r = get(
        url,
        params=params,
        headers={"Accept": "text/csv,application/vnd.sdmx.data+csv;q=0.9,*/*;q=0.1"},
    )
    return _parse_sdmx_csv_response(r, flow, codes, key)


def fetch(flow, codes, start, end=None):
    """Download one ADB v5 chunk, with a smaller-economy fallback for KIDB 5xx/429.

    Normal path is unchanged: one request for all economies.  If KIDB repeatedly
    rejects that request with a retryable HTTP status (notably 503), retry the
    same LOCKED indicators in 10-economy chunks.  This preserves full historical
    refresh while reducing server-side query size.  When the API also rejects a
    chunk with an open-ended end year, retry only that chunk with the currently
    documented KIDB maximum endPeriod (2024).
    """
    if flow in SKIP_DATA_FLOWS:
        raise RuntimeError(
            f"{flow} is an umbrella/non-observation registry flow and is skipped."
        )

    key = _sdmx_key(flow, codes)
    try:
        return _request_sdmx(flow, codes, key, start, end)
    except Exception as exc:
        status = _http_status(exc)
        if status not in ADB_RETRYABLE_STATUS:
            raise
        print(
            f"    ADB HTTP {status} on all-economy request; "
            f"falling back to economy chunks of {ADB_ECONOMY_CHUNK_SIZE}.",
            flush=True,
        )

    parts = []
    econ_chunks = list(chunks(ADB_ECONOMY_CODES, ADB_ECONOMY_CHUNK_SIZE))
    for i, economies in enumerate(econ_chunks, 1):
        econ_key = _sdmx_key_for_economies(flow, codes, economies)
        try:
            z = _request_sdmx(flow, codes, econ_key, start, end)
        except Exception as exc:
            status = _http_status(exc)
            if status not in ADB_RETRYABLE_STATUS or end is not None:
                raise
            print(
                f"      economy chunk {i}/{len(econ_chunks)} HTTP {status}; "
                f"retrying with explicit endPeriod={ADB_DOCUMENTED_MAX_YEAR}.",
                flush=True,
            )
            z = _request_sdmx(
                flow, codes, econ_key, start, ADB_DOCUMENTED_MAX_YEAR
            )
        if z is not None and not z.empty:
            parts.append(z)
        print(
            f"      economy chunk {i}/{len(econ_chunks)}: rows={0 if z is None else len(z):,}",
            flush=True,
        )
        if i < len(econ_chunks):
            time.sleep(REQUEST_PAUSE_SECONDS)

    if not parts:
        raise RuntimeError(
            f"ADB economy-chunk fallback returned zero rows for flow={flow}, codes={codes}"
        )
    out = pd.concat(parts, ignore_index=True, sort=False)
    out = out.drop_duplicates().reset_index(drop=True)
    print(
        f"    ADB economy-chunk fallback DONE: rows={len(out):,}",
        flush=True,
    )
    return out


def _ordered_flows(preferred, all_flows):
    """
    Build a registry-safe discovery queue.

    Older common.py files can contain legacy preferred flow IDs such as
    GG or GG_GF. Current ADB KIDB v5 queries should use IDs returned by
    the live v5 registry (typically DF_*). Legacy IDs that are not in
    the current registry are skipped instead of queried.
    """
    registry = [str(x).strip() for x in all_flows if str(x).strip()]
    registry_set = set(registry)

    preferred_valid = []
    preferred_skipped = []
    for x in preferred or []:
        x = str(x).strip()
        if not x:
            continue
        if x in registry_set:
            preferred_valid.append(x)
        else:
            preferred_skipped.append(x)

    current_df = [
        x for x in registry
        if x.startswith("DF_") and x not in SKIP_DATA_FLOWS
    ]
    other_registry = [
        x for x in registry
        if not x.startswith("DF_") and x not in SKIP_DATA_FLOWS
    ]

    ordered = []
    seen = set()
    for x in [*preferred_valid, *current_df, *other_registry]:
        if x not in seen:
            ordered.append(x)
            seen.add(x)

    return ordered, preferred_skipped



def _annual_locked_direct(cfg):
    """Fast annual mode: exact LOCKED ADB flow+indicator codes only."""
    from common import load_locked_plan
    plan = load_locked_plan(SOURCE)
    rows = pd.DataFrame(plan.get("rows") or []).fillna("")
    if rows.empty:
        raise RuntimeError("ADB annual LOCKED plan has no rows.")
    # Accept both canonical registry fields and review-workbook aliases.
    ren = {
        "ADB Flow":"SOURCE_DATAFLOW", "flow":"SOURCE_DATAFLOW",
        "ADB Indicator Code":"ADB_INDICATOR_CODE", "adb_indicator_code":"ADB_INDICATOR_CODE",
        "Fiscal Survey Code":"wbfd_mnemonic", "Fiscal Survey Variable":"wbfd_variable",
    }
    rows = rows.rename(columns={k:v for k,v in ren.items() if k in rows.columns})
    needed={"SOURCE_DATAFLOW","ADB_INDICATOR_CODE","wbfd_mnemonic"}
    miss=needed-set(rows.columns)
    if miss: raise RuntimeError(f"ADB LOCKED plan missing {sorted(miss)}")
    rows["SOURCE_DATAFLOW"] = rows["SOURCE_DATAFLOW"].astype(str).str.strip()
    rows["ADB_INDICATOR_CODE"] = rows["ADB_INDICATOR_CODE"].astype(str).str.strip()
    rows["wbfd_mnemonic"] = rows["wbfd_mnemonic"].astype(str).str.strip().str.upper()
    if "wbfd_variable" not in rows: rows["wbfd_variable"]=""
    rows=rows[(rows.SOURCE_DATAFLOW!='')&(rows.ADB_INDICATOR_CODE!='')&(rows.wbfd_mnemonic!='')].copy()
    if rows.empty: raise RuntimeError("ADB LOCKED plan has zero exact flow+indicator mappings.")

    root = Path(f"adb_fiscal_data_latest_{run_stamp()}")
    data, meta, logs = root/"data", root/"metadata", root/"logs"
    for d in (data,meta,logs): d.mkdir(parents=True,exist_ok=True)
    rows.to_csv(meta/"adb_locked_registry_used.csv",index=False,encoding="utf-8-sig")
    outputs=[]; audit=[]
    chunk_size=min(max(int(cfg.get("query_chunk_size",20)),1),20)
    for flow_no,(flow,rr) in enumerate(rows.groupby("SOURCE_DATAFLOW",sort=True),1):
        codes=rr["ADB_INDICATOR_CODE"].drop_duplicates().tolist()
        print(f"  ADB DIRECT LOCKED [{flow_no}] {flow}: indicators={len(codes)}",flush=True)
        lookup=rr[["ADB_INDICATOR_CODE","wbfd_mnemonic","wbfd_variable"]].drop_duplicates()
        for part,code_chunk in enumerate(chunks(codes,chunk_size),1):
            try:
                raw=fetch(flow,code_chunk,cfg["start_year"],cfg.get("end_year"))
                ccol=first(raw,COUNTRY_CANDIDATES); ycol=first(raw,YEAR_CANDIDATES); vcol=first(raw,VALUE_CANDIDATES)
                icol=first(raw,INDICATOR_CANDIDATES); ucol=first(raw,UNIT_CANDIDATES)
                if not all([ccol,ycol,vcol,icol]):
                    raise RuntimeError(f"ADB direct response missing required fields country={ccol}, year={ycol}, value={vcol}, indicator={icol}")
                raw=filter_years(raw,ycol,SOURCE)
                raw["ADB_INDICATOR_CODE"] = raw[icol].astype(str).str.strip()
                raw["SOURCE_DATAFLOW"] = flow
                raw=raw.merge(lookup,on="ADB_INDICATOR_CODE",how="inner",validate="many_to_many")
                if raw.empty: raise RuntimeError("ADB direct LOCKED merge produced zero rows")
                raw["_indicator_label"] = raw["ADB_INDICATOR_CODE"]
                std=standardize(raw,"ADB_KIDB",ccol,ycol,vcol,"_indicator_label",ucol,None)
                std["SOURCE_DATAFLOW"] = flow
                std["ADB_INDICATOR_CODE"] = raw["ADB_INDICATOR_CODE"].to_numpy()
                std["wbfd_mnemonic"] = raw["wbfd_mnemonic"].to_numpy()
                std["wbfd_variable"] = raw["wbfd_variable"].to_numpy()
                std["FISCAL_SURVEY_CODE"] = std["wbfd_mnemonic"]
                std["FISCAL_SURVEY_VARIABLE"] = std["wbfd_variable"]
                std["mapping_status"]="LOCKED_DIRECT"; std["mapping_confidence"]="HIGH"; std["mapping_method"]="ADB_LOCKED_FLOW_INDICATOR_DIRECT"
                std["source_dataset"] = flow
                sf=data/f"ADB_DIRECT_{safe_name(flow) if 'safe_name' in globals() else re.sub(r'[^A-Za-z0-9_-]+','_',flow)}_{part:02d}_standardized.csv"
                std.to_csv(sf,index=False,encoding="utf-8-sig")
                outputs.append(std)
                audit.append({"flow":flow,"part":part,"requested_indicators":len(code_chunk),"rows":len(std),"status":"SUCCESS"})
                print(f"    part {part}: rows={len(std):,}",flush=True)
            except Exception as e:
                audit.append({"flow":flow,"part":part,"requested_indicators":len(code_chunk),"rows":0,"status":"FAILED","message":str(e)})
                raise
            if part < (len(codes)+chunk_size-1)//chunk_size: time.sleep(REQUEST_PAUSE_SECONDS)
    prod=pd.concat(outputs,ignore_index=True,sort=False) if outputs else pd.DataFrame()
    prod.to_csv(data/"adb_DIRECT_LOCKED_PRODUCTION.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(audit).to_csv(logs/"download_summary_latest.csv",index=False,encoding="utf-8-sig")
    print(f"  ADB DIRECT LOCKED DONE: rows={len(prod):,}; codes={prod['wbfd_mnemonic'].nunique() if not prod.empty else 0}")
    return pd.DataFrame(audit)

def main():
    cfg = source_settings(SOURCE)
    # CRITICAL: annual production mode must NEVER fall back to broad ADB discovery.
    # The direct routine validates the LOCKED plan itself and fails fast if exact
    # SOURCE_DATAFLOW + ADB_INDICATOR_CODE signatures are unavailable.
    if os.environ.get("WBFD_ANNUAL_LOCKED_MODE", "").strip() == "1":
        print("  ADB ANNUAL LOCKED mode: discovery is disabled; exact LOCKED flow+indicator requests only", flush=True)
        return _annual_locked_direct(cfg)
    print("  ADB standalone mode: no input mapping Excel is required")
    root = Path(f"adb_fiscal_data_latest_{run_stamp()}")
    data, meta, logs = root / "data", root / "metadata", root / "logs"
    for p in (data, meta, logs):
        p.mkdir(parents=True, exist_ok=True)

    cfg_terms = cfg.get("fiscal_keywords", []) or []
    discovery_terms = list(dict.fromkeys([*cfg_terms, *FISCAL_SEARCH_TERMS]))

    # ------------------------------------------------------------------
    # 1) Discover ALL ADB dataflows.
    # ------------------------------------------------------------------
    print("  ADB: discovering all KIDB dataflows ...")
    try:
        all_flow_ids = discover_all_dataflows()
    except Exception as e:
        (logs / "all_dataflows_discovery_error.txt").write_text(str(e), encoding="utf-8")
        # Preserve the old preferred-flow behavior as a last-resort fallback.
        all_flow_ids = list(cfg.get("preferred_flows", []))

    preferred = list(cfg.get("preferred_flows", []))
    flows_to_scan, skipped_legacy_flows = _ordered_flows(preferred, all_flow_ids)

    if skipped_legacy_flows:
        print(
            "  ADB: skipping legacy/non-v5 preferred flow IDs from common.py: "
            + ", ".join(skipped_legacy_flows)
        )
        print("       Discovery will use only IDs returned by the current ADB v5 registry.")

    pd.DataFrame({"flow": flows_to_scan}).to_csv(
        meta / "all_dataflows_scanned.csv", index=False, encoding="utf-8-sig"
    )
    print(f"  ADB: {len(flows_to_scan)} dataflows queued for indicator discovery")

    # ------------------------------------------------------------------
    # 2) Scan every dataflow for Fiscal Survey-related indicators.
    #    IMPORTANT: finding matches in GG/GG_GF does NOT stop discovery.
    # ------------------------------------------------------------------
    allmeta = []
    flows = []
    discovery_log = []

    for idx, flow in enumerate(flows_to_scan, 1):
        try:
            inds = get_indicators(flow)
            matches = fiscal(inds, discovery_terms)
            discovery_log.append({
                "flow": flow,
                "indicator_count": len(inds),
                "fiscal_candidate_count": len(matches),
                "status": "SUCCESS",
                "message": "",
            })
            print(
                f"  [{idx:02d}/{len(flows_to_scan):02d}] {flow}: "
                f"indicators={len(inds)}, fiscal_candidates={len(matches)}"
            )
            if matches:
                flows.append((flow, matches))
                allmeta.extend(matches)
        except Exception as e:
            discovery_log.append({
                "flow": flow,
                "indicator_count": 0,
                "fiscal_candidate_count": 0,
                "status": "FAILED",
                "message": str(e),
            })
            print(f"  [{idx:02d}/{len(flows_to_scan):02d}] {flow}: discovery FAILED: {e}")

        # ADB documents a 20-query/minute limit.
        if idx < len(flows_to_scan):
            time.sleep(REQUEST_PAUSE_SECONDS)

    discovery_log_df = pd.DataFrame(discovery_log)
    discovery_log_df.to_csv(
        logs / "dataflow_discovery_summary.csv", index=False, encoding="utf-8-sig"
    )

    fiscal_inventory = pd.DataFrame(allmeta)
    if not fiscal_inventory.empty:
        fiscal_inventory = fiscal_inventory.drop_duplicates(
            subset=["flow", "code"], keep="first"
        ).sort_values(["flow", "code"])
    fiscal_inventory.to_csv(
        meta / "discovered_fiscal_indicators_all_flows.csv",
        index=False,
        encoding="utf-8-sig",
    )

    if not flows:
        raise RuntimeError(
            "No Fiscal Survey-related ADB indicators were found after scanning all available KIDB dataflows."
        )

    # ------------------------------------------------------------------
    # 3) Apply embedded Fiscal Survey mapping rules to every discovered ADB
    #    indicator. Exact code mappings rank first; new text-derived matches
    #    remain reviewable AUTO_CANDIDATE/CONDITIONAL/PARTIAL statuses.
    # ------------------------------------------------------------------
    print("\n  ADB: loading embedded Fiscal Survey mapping rules (no input Excel required)")
    mapping_rules = load_mapping_rules()
    mapping_candidates, mapping_best = build_mapping_candidates(
        fiscal_inventory, mapping_rules
    )
    mapping_candidates.to_csv(
        meta / "adb_to_fiscal_mapping_candidates.csv",
        index=False, encoding="utf-8-sig"
    )
    mapping_best.to_csv(
        meta / "adb_to_fiscal_mapping_best.csv",
        index=False, encoding="utf-8-sig"
    )

    discovered_keys = fiscal_inventory[["flow", "code", "label", "description"]].copy()
    best_keys = mapping_best[[
        "flow", "adb_indicator_code", "fiscal_survey_code",
        "fiscal_survey_variable", "mapping_status", "mapping_confidence",
        "match_type", "mapping_score", "candidate_rank"
    ]].copy() if not mapping_best.empty else pd.DataFrame()
    if not best_keys.empty:
        discovered_keys = discovered_keys.merge(
            best_keys,
            left_on=["flow", "code"],
            right_on=["flow", "adb_indicator_code"],
            how="left"
        )
    discovered_keys.to_csv(
        meta / "discovered_fiscal_indicators_with_mapping.csv",
        index=False, encoding="utf-8-sig"
    )
    print(
        f"  ADB mapping: indicators={len(fiscal_inventory):,}, "
        f"mapped_best={len(mapping_best):,}, candidate_links={len(mapping_candidates):,}"
    )

    # Generate the ACTUAL runtime mapping workbook from scratch.
    runtime_xlsx = meta / "ADB_KIDB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx"
    runtime_stats = export_runtime_mapping_workbook(
        runtime_xlsx,
        fiscal_inventory,
        mapping_candidates,
        mapping_best,
        mapping_rules,
        discovery_log_df,
    )
    print(
        "  ADB runtime workbook: "
        f"discovered={runtime_stats['discovered_indicators']:,}, "
        f"mapped ADB indicators={runtime_stats['mapped_adb_indicators']:,}, "
        f"mapped Fiscal Survey codes={runtime_stats['mapped_fiscal_survey_codes']:,}, "
        f"gaps={runtime_stats['gap_fiscal_survey_codes']:,}"
    )
    print(f"  Runtime mapping workbook: {runtime_xlsx.resolve()}")

    # ------------------------------------------------------------------
    # 4) Download ALL discovered fiscal candidates by flow, in small chunks.
    # ------------------------------------------------------------------
    summary = []
    query_chunk_size = min(int(cfg.get("query_chunk_size", 15)), 20)

    for flow, matches in flows:
        # Deduplicate within each flow.
        dedup = {x["code"]: x for x in matches}
        matches = list(dedup.values())
        labelmap = {x["code"]: x["label"] for x in matches}
        codes = [x["code"] for x in matches]
        flow_mapping_lookup = _mapping_lookup(mapping_best, flow)

        print(f"\n  ADB {flow}: downloading {len(codes)} fiscal candidate indicators")

        for part, code_chunk in enumerate(chunks(codes, query_chunk_size), 1):
            name = f"{flow}_fiscal_part{part:02d}"
            try:
                raw = fetch(flow, code_chunk, cfg["start_year"], cfg.get("end_year"))

                # Always save response shape before validation.
                pd.DataFrame({"column": list(raw.columns)}).to_csv(
                    meta / f"{name}_columns.csv", index=False, encoding="utf-8-sig"
                )
                raw.head(20).to_csv(
                    meta / f"{name}_sample20.csv", index=False, encoding="utf-8-sig"
                )

                ccol = first(raw, COUNTRY_CANDIDATES)
                ycol = first(raw, YEAR_CANDIDATES)
                vcol = first(raw, VALUE_CANDIDATES)
                icol = first(raw, INDICATOR_CANDIDATES)
                ucol = first(raw, UNIT_CANDIDATES)

                if not all([ccol, ycol, vcol]):
                    raise ValueError(
                        f"Required observation columns not identified: "
                        f"country={ccol}, year={ycol}, value={vcol}; "
                        f"available={list(raw.columns)}"
                    )

                raw = filter_years(raw, ycol, SOURCE)
                qc = validate_observations(raw, SOURCE, name, ccol, ycol, vcol)

                raw["_indicator_label"] = (
                    raw[icol].map(labelmap).fillna(raw[icol]) if icol else name
                )
                # Attach Fiscal Survey mapping metadata before and after
                # standardization. New candidates remain explicitly marked
                # as candidates/review items rather than silently approved.
                if icol:
                    indicator_codes = raw[icol].astype(str).tolist()
                else:
                    indicator_codes = [""] * len(raw)
                raw = annotate_mapping(raw, indicator_codes, flow_mapping_lookup)

                std = standardize(
                    raw, "ADB_KIDB", ccol, ycol, vcol, "_indicator_label", ucol, None
                )
                if len(std) == len(indicator_codes):
                    std = annotate_mapping(std, indicator_codes, flow_mapping_lookup)
                else:
                    # Defensive fallback in case standardize() changes row count.
                    std["FISCAL_SURVEY_CODE"] = ""
                    std["FISCAL_SURVEY_VARIABLE"] = ""
                    std["MAPPING_STATUS"] = "ROW_COUNT_CHANGED_REVIEW"
                    std["MAPPING_CONFIDENCE"] = ""
                    std["MAPPING_MATCH_TYPE"] = ""
                    std["MAPPING_SCORE"] = ""

                stamp = datetime.now().isoformat(timespec="seconds")
                for f in (raw, std):
                    f["SOURCE_DATAFLOW"] = flow
                    f["DOWNLOAD_TIMESTAMP"] = stamp

                rf = data / f"{name}_raw.csv"
                sf = data / f"{name}_standardized.csv"
                raw.to_csv(rf, index=False, encoding="utf-8-sig")
                std.to_csv(sf, index=False, encoding="utf-8-sig")

                summary.append({
                    "dataset": name,
                    "flow": flow,
                    "requested_indicators": len(code_chunk),
                    **qc,
                    "status": "SUCCESS",
                    "file": str(sf),
                })
                print(
                    f"    SUCCESS {name}: requested={len(code_chunk)}, "
                    f"rows={qc['rows']:,}, countries={qc['countries']}, "
                    f"years={qc['actual_min_year']}-{qc['actual_max_year']}"
                )
            except Exception as e:
                print(f"    FAILED {name}: {e}")
                summary.append({
                    "dataset": name,
                    "flow": flow,
                    "requested_indicators": len(code_chunk),
                    "rows": 0,
                    "status": "FAILED",
                    "message": str(e),
                })

            time.sleep(REQUEST_PAUSE_SECONDS)

    s = pd.DataFrame(summary)
    s.to_csv(logs / "download_summary_latest.csv", index=False, encoding="utf-8-sig")

    # Convenient unique-indicator inventory for mapping review.
    if not fiscal_inventory.empty:
        unique_inventory = (
            fiscal_inventory
            .sort_values(["code", "flow"])
            .groupby("code", as_index=False)
            .agg({
                "label": "first",
                "description": "first",
                "metadata_unit": "first",
                "matched_terms": "first",
                "flow": lambda x: " | ".join(sorted(set(map(str, x)))),
            })
            .rename(columns={"flow": "available_in_flows"})
        )
        unique_inventory.to_csv(
            meta / "discovered_fiscal_indicators_unique.csv",
            index=False,
            encoding="utf-8-sig",
        )

    print(f"\nADB output root: {root.resolve()}")
    print(f"Fiscal candidate inventory: {meta / 'discovered_fiscal_indicators_all_flows.csv'}")
    print(f"Unique fiscal indicators:   {meta / 'discovered_fiscal_indicators_unique.csv'}")
    print(f"Mapping candidates:         {meta / 'adb_to_fiscal_mapping_candidates.csv'}")
    print(f"Best indicator mappings:    {meta / 'adb_to_fiscal_mapping_best.csv'}")
    return s


if __name__ == "__main__":
    main()
