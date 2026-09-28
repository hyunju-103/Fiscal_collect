
from __future__ import annotations
from pathlib import Path
from datetime import datetime
import importlib
import time
import requests
import pandas as pd

try:
    import pycountry
except ImportError:
    pycountry = None

SOURCE_SETTINGS = {
    "OECD": {
        "start_year": 2007, "end_year": None, "latest_vintage": True, "enabled": True
    },
    "ECLAC": {
        "start_year": 2007, "end_year": None, "enabled": True,
        "indicators": {
            "tax_revenue_by_type_lcu": 845,
            "government_operations_lcu": 1259,
            "public_debt_usd_mn": 1239,
            "natural_resource_revenue_lcu": 3353,
            "public_spending_by_function_lcu": 4409,
            "social_expenditure_cofog_lcu": 3126,
        },
    },
    "EUROSTAT": {
        "start_year": 2007, "end_year": None, "enabled": True,
        "datasets": ["gov_10a_main","gov_10a_taxag","gov_10a_exp","gov_10dd_edpt1","gov_10q_ggdebt"],
    },
    "ADB": {
        "start_year": 2007, "end_year": None, "enabled": True,
        "preferred_flows": ["GG_GF","GG"],
        "fallback_discovery": True,
        "query_chunk_size": 20,
        "fiscal_keywords": [
            "government revenue","tax revenue","government taxes",
            "government expenditure","government expense",
            "net lending","net borrowing","fiscal balance","budget balance",
            "interest payment","social protection","social security",
            "public debt","government debt","external debt"
        ],
    },
    "AFDB": {
        "start_year": 2007, "end_year": None, "enabled": True,
        "dataset_id": "nbyenxf",
        "api_host": "https://dataportal.opendataforafrica.org",
        "fiscal_keywords": [
            "revenue","tax","grant","expenditure","expense","wage","compensation",
            "interest","fiscal balance","budget balance","deficit","surplus",
            "financing","public debt","government debt","external debt","domestic debt"
        ],
    },
    "IDB": {
        "start_year": 2007, "end_year": None, "enabled": True,
        "latest_vintage": True,
        "latin_macro_watch_package": "latin-macro-watch-dataset",
        "latin_macro_watch_resource_name": "Latin Macro Watch - Dataset",
        "debt_package_query": "Latin America Caribbean Standardized Public Debt Database",
    },
    "CIAT": {
        "start_year": 2007, "end_year": None, "enabled": True,
        "mode": "explore",
        "source_pages": [
            "https://www.ciat.org/ciatdata/?lang=en",
            "https://www.ciat.org/tax-statistics-oecd-ciat-idb-eclac/?lang=en",
            "https://www.ciat.org/base-de-datos-de-recaudacion-bid-ciat/"
        ],
    },
    "BCEAO": {
        "start_year": 2007, "end_year": None, "enabled": True,
        "mode": "explore",
        "portal": "https://www.edenpub.bceao.int/en",
        "fiscal_scope": ["TOFE","DETTE PUBLIQUE EXTERIEURE"],
    },
    "CEMAC": {
        "start_year": 2007, "end_year": None, "enabled": True,
        "mode": "explore",
        "portal": "https://www.beac.int/bdemfcemac",
        "info_page": "https://www.beac.int/economie-stats/base-de-donnees-economiques-monetaires-financieres/",
        "public_login": "public",
        "public_password": "public",
    },
    "AMF": {
        "start_year": 2007, "end_year": None, "enabled": True,
        "mode": "explore",
        "portal": "https://www.amf.org.ae/en/arabic_economic_database",
        "info_page": "https://www.amf.org.ae/en/statistics",
    },
}

REQUEST_SETTINGS = {"timeout": 300, "retries": 3, "sleep": 5}
HEADERS = {"User-Agent": "WorldBank-BetterData/3.0", "Accept": "*/*"}

def source_settings(source):
    return dict(SOURCE_SETTINGS[source.upper()])

def request_settings():
    return dict(REQUEST_SETTINGS)

def get(url, params=None, headers=None, timeout=None, retries=None, sleep=None, stream=False):
    cfg = request_settings()
    timeout = cfg["timeout"] if timeout is None else timeout
    retries = cfg["retries"] if retries is None else retries
    sleep = cfg["sleep"] if sleep is None else sleep
    h = HEADERS.copy()
    if headers: h.update(headers)
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=timeout, stream=stream)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if i < retries-1: time.sleep(sleep*(i+1))
    raise last

def post(url, json_data=None, data=None, headers=None, timeout=None, retries=None, sleep=None):
    cfg = request_settings()
    timeout = cfg["timeout"] if timeout is None else timeout
    retries = cfg["retries"] if retries is None else retries
    sleep = cfg["sleep"] if sleep is None else sleep
    h = HEADERS.copy()
    if headers: h.update(headers)
    last = None
    for i in range(retries):
        try:
            r = requests.post(url, json=json_data, data=data, headers=h, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if i < retries-1: time.sleep(sleep*(i+1))
    raise last

def wb_like_country_code(x):
    if x is None or pd.isna(x): return None
    s = str(x).strip()
    aliases = {
        "Taipei,China":"TWN","China, People's Republic of":"CHN",
        "Korea, Republic of":"KOR","Türkiye":"TUR",
        "Bolivia (Plurinational State of)":"BOL","Venezuela (Bolivarian Republic of)":"VEN"
    }
    if s in aliases: return aliases[s]
    if len(s)==3 and s.isalpha(): return s.upper()
    if pycountry is None: return None
    try:
        c = pycountry.countries.get(alpha_2=s.upper()) if len(s)==2 else pycountry.countries.lookup(s)
        return c.alpha_3 if c else None
    except Exception:
        return None

def extract_year(values):
    s = values.astype("string").str.strip()
    y = pd.to_numeric(s.str.extract(r"((?:19|20)\d{2})", expand=False), errors="coerce")
    return y.fillna(pd.to_numeric(values, errors="coerce")).astype("Float64")

def filter_years(df, year_col, source):
    if not year_col or year_col not in df.columns: return df.copy()
    cfg = source_settings(source)
    y = extract_year(df[year_col])
    mask = y.ge(cfg["start_year"])
    if cfg.get("end_year") is not None: mask &= y.le(cfg["end_year"])
    return df.loc[mask.fillna(False)].copy()

def standardize(df, source, country_col=None, year_col=None, value_col=None,
                indicator_col=None, unit_col=None, currency_col=None):
    out = pd.DataFrame(index=df.index)
    out["source"] = source
    out["country_original"] = df[country_col] if country_col and country_col in df.columns else None
    out["country_code"] = out["country_original"].map(wb_like_country_code) if country_col else None
    out["year"] = extract_year(df[year_col]).round().astype("Int64") if year_col and year_col in df.columns else pd.NA
    out["indicator_original"] = df[indicator_col] if indicator_col and indicator_col in df.columns else None
    out["unit_original"] = df[unit_col] if unit_col and unit_col in df.columns else None
    out["currency_original"] = df[currency_col] if currency_col and currency_col in df.columns else None
    out["value_original"] = pd.to_numeric(df[value_col], errors="coerce") if value_col and value_col in df.columns else pd.NA
    out["value_lcu_mn"] = pd.NA
    out["wbfd_mnemonic"] = pd.NA
    out["conversion_note"] = pd.NA
    return out

def validate_observations(df, source, dataset, country_col, year_col, value_col):
    if df is None or df.empty:
        raise RuntimeError(f"{source}/{dataset}: zero rows.")
    missing=[n for n,c in [("country",country_col),("year",year_col),("value",value_col)]
             if not c or c not in df.columns]
    if missing:
        raise RuntimeError(f"{source}/{dataset}: missing required fields: {', '.join(missing)}. Columns={list(df.columns)[:50]}")
    y=extract_year(df[year_col])
    v=pd.to_numeric(df[value_col],errors="coerce")
    c=df[country_col].astype("string").str.strip()
    valid=y.notna() & v.notna() & c.notna() & c.ne("") & c.ne("<NA>")
    if valid.sum()==0:
        raise RuntimeError(f"{source}/{dataset}: no rows with country + year + numeric value.")
    return {
        "rows":int(len(df)),"valid_observations":int(valid.sum()),
        "countries":int(c[valid].nunique()),
        "actual_min_year":int(y[valid].min()),"actual_max_year":int(y[valid].max()),
        "missing_value_rate":float(1-v.notna().mean()),
    }

def run_stamp():
    return datetime.now().strftime("%Y%m%d")

def _module_status(result):
    if isinstance(result,pd.DataFrame) and "status" in result.columns and not result.empty:
        st=set(result["status"].dropna().astype(str).str.upper())
        if st=={"SUCCESS"}: return "SUCCESS",""
        if "SUCCESS" in st or any(x.startswith("PARTIAL") for x in st): return "PARTIAL","; ".join(sorted(st))
        return "FAILED","; ".join(sorted(st))
    return "SUCCESS",""


RUN_MODULES = [
    ("OECD","oecd_main","oecd_data_*"),
    ("ECLAC","eclac_main","eclac_data_latest_*"),
    ("EUROSTAT","eurostat_main","eurostat_data_latest_*"),
    ("ADB","adb_main","adb_fiscal_data_latest_*"),
    ("AFDB","afdb_aih_main","afdb_fiscal_data_latest_*"),
    ("IDB","idb_main","idb_fiscal_data_latest_*"),
    ("AMF","amf_main","amf_source_exploration_*"),
]


def _module_status(result):
    if result is None:
        return "SUCCESS", ""
    if not isinstance(result, pd.DataFrame) or result.empty:
        return "SUCCESS", ""
    if "status" not in result.columns:
        return "SUCCESS", ""
    vals = sorted(set(result["status"].dropna().astype(str)))
    if not vals:
        return "SUCCESS", ""
    if vals == ["SUCCESS"]:
        return "SUCCESS", ""
    if "SUCCESS" in vals:
        return "PARTIAL", "; ".join(vals)
    return "FAILED", "; ".join(vals)


def _latest(pattern):
    xs=[p for p in Path(".").glob(pattern) if p.is_dir()]
    return max(xs,key=lambda p:p.stat().st_mtime) if xs else None


def run_all():
    rows=[]
    roots={}
    here=Path(__file__).resolve().parent

    print("="*100)
    print("WBFD SOURCE-SPECIFIC FISCAL SURVEY PIPELINE")
    print("="*100)

    for source,module_name,pattern in RUN_MODULES:
        if not (here/f"{module_name}.py").exists():
            rows.append({"source":source,"status":"SKIPPED","message":"module not present"})
            continue
        cfg=source_settings(source)
        if not cfg.get("enabled",True):
            continue
        print(f"\n[{source}] start={cfg['start_year']} end={cfg.get('end_year')} mode={cfg.get('mode','collector')}")
        try:
            result=importlib.import_module(module_name).main()
            status,msg=_module_status(result)
            root=_latest(pattern)
            if root:
                roots[source]=root
            rows.append({"source":source,"status":status,"message":msg,"output_root":str(root or "")})
            print(f"[{source}] {status}" + (f": {msg}" if msg else ""))
        except Exception as e:
            rows.append({"source":source,"status":"FAILED","message":str(e),"output_root":""})
            print(f"[{source}] FAILED: {e}")

    run_df=pd.DataFrame(rows)
    run_df.to_csv(
        Path(f"wbfd_collection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"),
        index=False,encoding="utf-8-sig"
    )

    print("\n"+"="*100)
    print("SOURCE-SPECIFIC SURVEY MAPPING")
    print("="*100)
    from survey_structural_mapper import aggregate
    master=aggregate(roots,"fiscal_survey_output")

    print("\n"+"="*100)
    print("FINAL")
    print("="*100)
    print(f"rows={len(master):,}")
    print(f"mnemonics={master['wbfd_mnemonic'].nunique() if not master.empty else 0}")
    print(f"countries={master['country_original'].nunique() if not master.empty else 0}")
    print(f"master={(Path('fiscal_survey_output')/'fiscal_survey_master_long.csv').resolve()}")
    return run_df


if __name__=="__main__":
    run_all()

# ---- FINAL selective/direct annual refresh ---------------------------------
# Mapping discovery remains a deliberate Step-1 activity.  During Step 3 the
# annual runner serializes the reviewed LOCKED registries into one JSON plan.
# Collectors read that plan and request only the exact source series needed for
# production, while still requesting the full historical time span.
_original_source_settings = source_settings

def load_locked_plan(source):
    import os, json
    pf = os.environ.get('WBFD_LOCKED_PLAN', '').strip()
    if not pf:
        return {}
    try:
        obj = json.loads(Path(pf).read_text(encoding='utf-8'))
        return obj.get(str(source).upper(), {}) or {}
    except Exception:
        return {}

def annual_locked_mode():
    import os
    return os.environ.get('WBFD_ANNUAL_LOCKED_MODE', '').strip() == '1'

def source_settings(source):
    cfg = _original_source_settings(source)
    import os
    if os.environ.get('WBFD_SELECTIVE_REFRESH') != '1':
        return cfg
    plan = load_locked_plan(source)
    src = str(source).upper()
    if plan.get('fiscal_codes'):
        cfg['locked_fiscal_codes'] = plan.get('fiscal_codes', [])
    if src == 'ADB':
        # In annual production mode ADB must never fall back to 65-flow discovery.
        # Exact source-signature validation happens inside adb_main._annual_locked_direct.
        cfg['locked_flows'] = plan.get('flows', [])
        cfg['locked_only'] = True
    elif src == 'EUROSTAT' and plan.get('datasets'):
        cfg['datasets'] = plan['datasets']; cfg['max_auto_datasets'] = 0; cfg['locked_only'] = True
    elif src == 'ECLAC' and plan.get('indicator_ids'):
        old = cfg.get('indicators', {}); rev = {str(v): k for k,v in old.items()}
        cfg['indicators'] = {rev.get(str(i), f'locked_{i}'): int(i) if str(i).isdigit() else i for i in plan['indicator_ids']}
        cfg['locked_only'] = True
    elif src == 'AFDB' and plan.get('dataset_ids'):
        cfg['locked_dataset_ids'] = plan['dataset_ids']; cfg['locked_only'] = True
    elif src == 'IDB':
        cfg['locked_only'] = True
    elif src == 'OECD' and plan.get('dataset_families'):
        cfg['locked_dataset_families'] = plan.get('dataset_families', []); cfg['locked_only'] = True
    elif src == 'AMF':
        cfg['locked_source_urls'] = plan.get('source_urls', []); cfg['locked_only'] = True
    return cfg
