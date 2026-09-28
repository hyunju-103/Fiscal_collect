from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

import pandas as pd


INSTITUTIONS = ["OECD", "ADB", "AFDB", "ECLAC", "EUROSTAT", "IDB"]

ALIASES = {
    "Fiscal Survey Code": "wbfd_mnemonic",
    "Fiscal Survey Variable": "wbfd_variable",
    "fiscal_survey_code": "wbfd_mnemonic",
    "fiscal_survey_variable": "wbfd_variable",
    "Country": "country_original",
    "country": "country_original",
    "Country Code": "country_code",
    "ISO3": "country_code",
    "iso3": "country_code",
    "Year": "year",
    "YEAR": "year",
    "Value": "value_original",
    "value": "value_original",
    "Unit": "unit_original",
    "unit": "unit_original",
    "Source Indicator": "indicator_original",
    "Indicator": "indicator_original",
}

REQUIRED_BASE = [
    "institution",
    "wbfd_mnemonic",
    "wbfd_variable",
    "country_code",
    "country_original",
    "year",
    "value_original",
    "unit_original",
    "indicator_original",
]


def txt(x) -> str:
    if pd.isna(x):
        return ""
    return str(x).strip()


def norm_unit(x) -> str:
    s = txt(x).lower()
    s = re.sub(r"\s+", " ", s)
    return s


def norm_country_name(x) -> str:
    s = txt(x).upper()
    s = re.sub(r"\s+", " ", s)
    return s


def infer_institution(path: Path) -> str:
    name = path.name.upper()
    for s in INSTITUTIONS:
        if name.startswith(s + "_") or f"_{s}_" in name:
            return s
    return ""


def find_annual_dir(base: Path, year: int | None) -> Path:
    annual_root = base / "annual_output"
    if year is not None:
        p = annual_root / str(year)
        if not p.exists():
            raise FileNotFoundError(f"Annual output folder not found: {p}")
        return p

    if not annual_root.exists():
        raise FileNotFoundError(f"annual_output folder not found: {annual_root}")

    candidates = [
        p for p in annual_root.iterdir()
        if p.is_dir() and p.name.isdigit()
    ]
    if not candidates:
        raise FileNotFoundError(f"No year folders found under: {annual_root}")

    return max(candidates, key=lambda p: int(p.name))


def standardize_file(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(path, low_memory=False)
    df = df.rename(
        columns={
            k: v
            for k, v in ALIASES.items()
            if k in df.columns and v not in df.columns
        }
    )

    inst_from_name = infer_institution(path)
    if "institution" not in df.columns:
        df["institution"] = inst_from_name
    else:
        df["institution"] = (
            df["institution"].fillna("").astype(str).str.strip().str.upper()
        )
        if inst_from_name:
            df.loc[df["institution"].eq(""), "institution"] = inst_from_name

    for c in REQUIRED_BASE:
        if c not in df.columns:
            df[c] = ""

    df["institution"] = df["institution"].astype(str).str.strip().str.upper()
    df["wbfd_mnemonic"] = (
        df["wbfd_mnemonic"].astype(str).str.strip().str.upper()
    )
    df["wbfd_variable"] = df["wbfd_variable"].fillna("").astype(str).str.strip()
    df["country_code"] = (
        df["country_code"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
        .replace({"NAN": "", "NONE": ""})
    )
    df["country_original"] = (
        df["country_original"].fillna("").astype(str).str.strip()
    )
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df["value_original"] = pd.to_numeric(df["value_original"], errors="coerce")
    df["unit_original"] = df["unit_original"].fillna("").astype(str).str.strip()
    df["indicator_original"] = (
        df["indicator_original"].fillna("").astype(str).str.strip()
    )

    # Prefer ISO/country code. Fall back to a normalized country name only when
    # a code is unavailable.
    df["country_key"] = df["country_code"]
    missing_code = df["country_key"].eq("")
    df.loc[missing_code, "country_key"] = (
        df.loc[missing_code, "country_original"].map(norm_country_name)
    )

    df["_unit_norm"] = df["unit_original"].map(norm_unit)
    df["_value_cmp"] = df["value_original"].round(10)
    df["input_file"] = path.name

    invalid = (
        df["institution"].eq("")
        | df["wbfd_mnemonic"].eq("")
        | df["country_key"].eq("")
        | df["year"].isna()
        | df["value_original"].isna()
    )

    bad = df.loc[invalid].copy()
    good = df.loc[~invalid].copy()

    return good, bad


def join_unique(values: Iterable[str]) -> str:
    vals = sorted({txt(x) for x in values if txt(x)})
    return " | ".join(vals)


def build_key_audit(df: pd.DataFrame) -> pd.DataFrame:
    key = ["country_key", "year", "wbfd_mnemonic"]

    def summarize(g: pd.DataFrame) -> pd.Series:
        sources = sorted(set(g["institution"].astype(str)))
        units = sorted({x for x in g["_unit_norm"].astype(str) if x})
        values = sorted(set(g["_value_cmp"].dropna().tolist()))

        n_sources = len(sources)
        n_units = len(units)
        n_values = len(values)

        if n_sources == 1 and n_units <= 1 and n_values == 1:
            status = "UNIQUE_SINGLE_SOURCE"
        elif n_sources > 1 and n_units <= 1 and n_values == 1:
            status = "CONSISTENT_MULTI_SOURCE"
        else:
            status = "CONFLICT"

        return pd.Series(
            {
                "n_rows": len(g),
                "n_sources": n_sources,
                "sources": " | ".join(sources),
                "n_units": n_units,
                "units": " | ".join(units),
                "n_values": n_values,
                "resolution_status": status,
                "country_original_examples": join_unique(
                    g["country_original"].tolist()
                ),
                "fiscal_survey_variable": next(
                    (
                        txt(x)
                        for x in g["wbfd_variable"].tolist()
                        if txt(x)
                    ),
                    "",
                ),
            }
        )

    return (
        df.groupby(key, dropna=False, sort=True)
        .apply(summarize, include_groups=False)
        .reset_index()
    )


def safe_resolved_long(
    df: pd.DataFrame, key_audit: pd.DataFrame
) -> pd.DataFrame:
    safe_keys = key_audit[
        key_audit["resolution_status"].ne("CONFLICT")
    ][
        [
            "country_key",
            "year",
            "wbfd_mnemonic",
            "sources",
            "n_sources",
            "resolution_status",
        ]
    ].copy()

    x = df.merge(
        safe_keys,
        on=["country_key", "year", "wbfd_mnemonic"],
        how="inner",
        validate="many_to_one",
    )

    # Multiple institutions may contain exactly the same value. Keep one row,
    # while preserving all contributing source names in `sources`.
    x = (
        x.sort_values(
            ["country_key", "year", "wbfd_mnemonic", "institution"]
        )
        .drop_duplicates(
            ["country_key", "year", "wbfd_mnemonic"], keep="first"
        )
        .reset_index(drop=True)
    )

    return x


def source_specific_safe(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    key = ["institution", "country_key", "year", "wbfd_mnemonic"]

    audit = (
        df.groupby(key, dropna=False, sort=True)
        .agg(
            n_rows=("value_original", "size"),
            n_values=("_value_cmp", "nunique"),
            n_units=("_unit_norm", "nunique"),
        )
        .reset_index()
    )
    audit["source_key_status"] = "OK"
    audit.loc[
        (audit["n_values"] > 1) | (audit["n_units"] > 1),
        "source_key_status",
    ] = "SOURCE_INTERNAL_CONFLICT"

    ok = audit[audit["source_key_status"].eq("OK")][key]
    safe = df.merge(ok, on=key, how="inner", validate="many_to_one")
    safe = (
        safe.sort_values(key)
        .drop_duplicates(key, keep="first")
        .reset_index(drop=True)
    )
    return safe, audit


def make_source_wide(source_safe: pd.DataFrame) -> pd.DataFrame:
    if source_safe.empty:
        return pd.DataFrame()

    x = source_safe.copy()
    x["column_name"] = (
        x["wbfd_mnemonic"].astype(str)
        + "__"
        + x["institution"].astype(str)
    )

    id_cols = ["country_key", "year"]
    name_map = (
        x.groupby(id_cols, dropna=False)["country_original"]
        .agg(lambda s: next((txt(v) for v in s if txt(v)), ""))
        .reset_index(name="country_original")
    )
    code_map = (
        x.groupby(id_cols, dropna=False)["country_code"]
        .agg(lambda s: next((txt(v) for v in s if txt(v)), ""))
        .reset_index(name="country_code")
    )

    wide = (
        x.pivot(
            index=id_cols,
            columns="column_name",
            values="value_original",
        )
        .reset_index()
    )
    wide.columns.name = None
    wide = wide.merge(code_map, on=id_cols, how="left")
    wide = wide.merge(name_map, on=id_cols, how="left")

    first = ["country_code", "country_original", "country_key", "year"]
    rest = [c for c in wide.columns if c not in first]
    return wide[first + sorted(rest)]


def make_resolved_wide(resolved: pd.DataFrame) -> pd.DataFrame:
    if resolved.empty:
        return pd.DataFrame()

    id_cols = ["country_key", "year"]
    name_map = (
        resolved.groupby(id_cols, dropna=False)["country_original"]
        .agg(lambda s: next((txt(v) for v in s if txt(v)), ""))
        .reset_index(name="country_original")
    )
    code_map = (
        resolved.groupby(id_cols, dropna=False)["country_code"]
        .agg(lambda s: next((txt(v) for v in s if txt(v)), ""))
        .reset_index(name="country_code")
    )

    wide = (
        resolved.pivot(
            index=id_cols,
            columns="wbfd_mnemonic",
            values="value_original",
        )
        .reset_index()
    )
    wide.columns.name = None
    wide = wide.merge(code_map, on=id_cols, how="left")
    wide = wide.merge(name_map, on=id_cols, how="left")

    first = ["country_code", "country_original", "country_key", "year"]
    rest = [c for c in wide.columns if c not in first]
    return wide[first + sorted(rest)]


def coverage_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "institution",
                "rows",
                "countries",
                "fiscal_codes",
                "min_year",
                "max_year",
            ]
        )
    return (
        df.groupby("institution", dropna=False)
        .agg(
            rows=("value_original", "size"),
            countries=("country_key", "nunique"),
            fiscal_codes=("wbfd_mnemonic", "nunique"),
            min_year=("year", "min"),
            max_year=("year", "max"),
        )
        .reset_index()
        .sort_values("institution")
    )


def coverage_matrix(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    m = (
        df.groupby(["wbfd_mnemonic", "institution"])
        .size()
        .unstack(fill_value=0)
        .reset_index()
    )
    for s in INSTITUTIONS:
        if s not in m.columns:
            m[s] = 0
    return m[
        ["wbfd_mnemonic"]
        + [s for s in INSTITUTIONS if s in m.columns]
    ]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Combine institution-level LOCKED Fiscal Survey production data "
            "without silently averaging conflicting sources."
        )
    )
    ap.add_argument("--base", default=".", help="Pipeline base folder")
    ap.add_argument(
        "--year",
        type=int,
        default=None,
        help="annual_output year; default = latest numeric folder",
    )
    ap.add_argument(
        "--require-all",
        action="store_true",
        help="Fail unless all seven institutions are present",
    )
    args = ap.parse_args()

    base = Path(args.base).resolve()
    annual_dir = find_annual_dir(base, args.year)
    inst_dir = annual_dir / "institution_data"
    if not inst_dir.exists():
        raise FileNotFoundError(
            f"institution_data folder not found: {inst_dir}"
        )

    output_dir = annual_dir / "final_merged"
    output_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(
        inst_dir.glob("*_fiscal_survey_country_year_PRODUCTION.csv")
    )
    if not files:
        raise FileNotFoundError(
            f"No institution production CSV files found in: {inst_dir}"
        )

    found = {infer_institution(p) for p in files} - {""}
    missing = [s for s in INSTITUTIONS if s not in found]

    print("=" * 78)
    print("FINAL FISCAL SURVEY MERGE")
    print("=" * 78)
    print(f"Annual folder: {annual_dir}")
    print(f"Input files: {len(files)}")
    print(f"Institutions found: {', '.join(sorted(found))}")
    if missing:
        print(f"Institutions not present: {', '.join(missing)}")
        if args.require_all:
            raise RuntimeError(
                "Missing required institutions: " + ", ".join(missing)
            )

    good_parts = []
    bad_parts = []

    for p in files:
        good, bad = standardize_file(p)
        good_parts.append(good)
        if not bad.empty:
            bad["source_file"] = p.name
            bad_parts.append(bad)
        print(
            f"  {p.name}: usable={len(good):,}, invalid={len(bad):,}"
        )

    master_raw = pd.concat(good_parts, ignore_index=True, sort=False)
    invalid = (
        pd.concat(bad_parts, ignore_index=True, sort=False)
        if bad_parts
        else pd.DataFrame()
    )

    # Remove exact duplicate observations only. Never average values.
    dedupe_key = [
        "institution",
        "country_key",
        "year",
        "wbfd_mnemonic",
        "value_original",
        "_unit_norm",
    ]
    before = len(master_raw)
    master = (
        master_raw.sort_values(dedupe_key)
        .drop_duplicates(dedupe_key, keep="first")
        .reset_index(drop=True)
    )
    exact_dupes_removed = before - len(master)

    key_audit = build_key_audit(master)
    conflicts_keys = key_audit[
        key_audit["resolution_status"].eq("CONFLICT")
    ].copy()

    conflict_rows = master.merge(
        conflicts_keys[
            ["country_key", "year", "wbfd_mnemonic"]
        ],
        on=["country_key", "year", "wbfd_mnemonic"],
        how="inner",
    )

    resolved = safe_resolved_long(master, key_audit)
    source_safe, source_audit = source_specific_safe(master)

    source_wide = make_source_wide(source_safe)
    resolved_wide = make_resolved_wide(resolved)

    coverage = coverage_summary(master)
    matrix = coverage_matrix(master)

    # Clean helper columns from user-facing long files.
    helper_cols = ["_unit_norm", "_value_cmp"]
    master_out = master.drop(
        columns=[c for c in helper_cols if c in master.columns]
    )
    resolved_out = resolved.drop(
        columns=[c for c in helper_cols if c in resolved.columns]
    )
    conflict_out = conflict_rows.drop(
        columns=[c for c in helper_cols if c in conflict_rows.columns]
    )

    master_out.to_csv(
        output_dir / "01_FISCAL_SURVEY_MASTER_LONG_ALL_SOURCES.csv",
        index=False,
        encoding="utf-8-sig",
    )
    resolved_out.to_csv(
        output_dir / "02_FISCAL_SURVEY_MASTER_LONG_SAFE_RESOLVED.csv",
        index=False,
        encoding="utf-8-sig",
    )
    key_audit.to_csv(
        output_dir / "03_COUNTRY_YEAR_CODE_RESOLUTION_AUDIT.csv",
        index=False,
        encoding="utf-8-sig",
    )
    conflict_out.to_csv(
        output_dir / "04_CROSS_SOURCE_CONFLICT_ROWS.csv",
        index=False,
        encoding="utf-8-sig",
    )
    source_wide.to_csv(
        output_dir / "05_COUNTRY_YEAR_WIDE_BY_SOURCE.csv",
        index=False,
        encoding="utf-8-sig",
    )
    resolved_wide.to_csv(
        output_dir / "06_COUNTRY_YEAR_WIDE_SAFE_RESOLVED.csv",
        index=False,
        encoding="utf-8-sig",
    )
    coverage.to_csv(
        output_dir / "07_INSTITUTION_COVERAGE_SUMMARY.csv",
        index=False,
        encoding="utf-8-sig",
    )
    matrix.to_csv(
        output_dir / "08_VARIABLE_INSTITUTION_COVERAGE_MATRIX.csv",
        index=False,
        encoding="utf-8-sig",
    )
    source_audit.to_csv(
        output_dir / "09_WITHIN_SOURCE_KEY_AUDIT.csv",
        index=False,
        encoding="utf-8-sig",
    )
    if not invalid.empty:
        invalid.to_csv(
            output_dir / "10_INVALID_INPUT_ROWS.csv",
            index=False,
            encoding="utf-8-sig",
        )

    summary = pd.DataFrame(
        [
            {"metric": "input_files", "value": len(files)},
            {"metric": "institutions_found", "value": len(found)},
            {"metric": "usable_input_rows", "value": before},
            {
                "metric": "exact_duplicate_rows_removed",
                "value": exact_dupes_removed,
            },
            {"metric": "master_long_rows", "value": len(master_out)},
            {
                "metric": "country_year_code_keys",
                "value": len(key_audit),
            },
            {
                "metric": "safe_resolved_keys",
                "value": int(
                    key_audit["resolution_status"].ne("CONFLICT").sum()
                ),
            },
            {
                "metric": "conflicting_keys",
                "value": len(conflicts_keys),
            },
            {
                "metric": "fiscal_survey_codes",
                "value": master["wbfd_mnemonic"].nunique(),
            },
            {
                "metric": "countries",
                "value": master["country_key"].nunique(),
            },
            {"metric": "min_year", "value": master["year"].min()},
            {"metric": "max_year", "value": master["year"].max()},
        ]
    )
    summary.to_csv(
        output_dir / "00_MERGE_SUMMARY.csv",
        index=False,
        encoding="utf-8-sig",
    )

    print("\nMERGE COMPLETED")
    print(f"  Master long rows        : {len(master_out):,}")
    print(f"  Country-year-code keys  : {len(key_audit):,}")
    print(
        "  Safe resolved keys      : "
        f"{int(key_audit['resolution_status'].ne('CONFLICT').sum()):,}"
    )
    print(f"  Cross-source conflicts  : {len(conflicts_keys):,}")
    print(f"  Fiscal Survey codes     : {master['wbfd_mnemonic'].nunique():,}")
    print(f"  Countries               : {master['country_key'].nunique():,}")
    print(f"  Years                   : {master['year'].min()}-{master['year'].max()}")
    print(f"\nOutput folder: {output_dir}")
    print(
        "Main file: "
        + str(output_dir / "01_FISCAL_SURVEY_MASTER_LONG_ALL_SOURCES.csv")
    )
    print(
        "Safe wide: "
        + str(output_dir / "06_COUNTRY_YEAR_WIDE_SAFE_RESOLVED.csv")
    )


if __name__ == "__main__":
    main()
