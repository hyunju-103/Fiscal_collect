# Fiscal Pipeline — All-in-One Rule Mapping Version

## One command

```bat
python RUN_ALL_FISCAL_PIPELINE.py
```

This package runs the six production institutions:

- OECD
- ADB
- AFDB
- ECLAC
- EUROSTAT
- IDB

AMF is intentionally excluded.

## Mapping policy

### OECD / ADB / AFDB / ECLAC / EUROSTAT
These institutions use the bundled reviewed mapping tables and immutable LOCKED registries in `mapping_registry/`. Annual runs refresh the data, not the mapping.

### IDB
IDB uses the fast Latin Macro Watch Annual-Series rule workflow selected by the user.

On every IDB refresh the pipeline:

1. Downloads only the fixed Latin Macro Watch Annual Series resource.
2. Downloads the full annual history because old observations can be revised.
3. Builds the Annual-Series inventory.
4. Applies the 64-variable Fiscal Survey rule registry.
5. Applies IDB-specific exact anchors.
6. Scores basis/unit and government scope.
7. Selects one best source series per Fiscal Survey code.
8. Rebuilds `mapping_tables/IDB_to_Fiscal_Survey_Mapping.xlsx` and flat mapping CSVs.
9. Attaches those selected mappings to the actual IDB observations.
10. Reuses the same mapped observations as IDB annual production — no second IDB download.

The mapping builder preserves the original mapping status (`PRODUCTION`, `REVIEW`, `GAP`) for audit. For the integrated annual dataset, every best mapped IDB series in `IDB_MAPPED_ONLY.csv` is marked `RULE_SELECTED`, because this package is explicitly configured to use the user-selected rule-based IDB mapping method.

## IDB mapping outputs

Each IDB refresh creates or updates:

```text
mapping_tables/
  IDB_to_Fiscal_Survey_Mapping.xlsx
  IDB_to_Fiscal_Survey_Mapping.csv
  IDB_MAPPED_ONLY.csv
  IDB_MAPPING_RULES.csv
  IDB_SOURCE_EXACT_ANCHORS.csv
  IDB_LOCK_REVIEW_TEMPLATE.csv

mapping_audit/IDB/
  IDB_ANNUAL_SERIES_INVENTORY.csv
  IDB_ALL_MAPPING_CANDIDATES.csv
  mapped_data/
    IDB_MAPPED_FULL_HISTORY.csv
    IDB_MAPPED_2007_LATEST.csv

mapping_registry/
  IDB_RULE_SELECTED_CURRENT.csv
```

The Excel workbook contains the rules, anchors, scoring logic, full 64-variable mapping, mapped-only table, review/gap tables, all candidates, and the source-series inventory.

## Annual data outputs

```text
annual_output/<year>/institution_data/
  OECD_fiscal_survey_country_year_PRODUCTION.csv
  ADB_fiscal_survey_country_year_PRODUCTION.csv
  AFDB_fiscal_survey_country_year_PRODUCTION.csv
  ECLAC_fiscal_survey_country_year_PRODUCTION.csv
  EUROSTAT_fiscal_survey_country_year_PRODUCTION.csv
  IDB_fiscal_survey_country_year_PRODUCTION.csv
```

Completed files in the current package/year are automatically skipped unless `--force` is supplied.

If one institution is temporarily unavailable, the runner records `REFRESH_FAILED`, continues with the remaining institutions, and performs a partial final merge. The failed institution can be rerun later.

## Final merge

The final merge runs automatically after collection.

Main outputs:

```text
annual_output/<year>/final_merged/
  01_FISCAL_SURVEY_MASTER_LONG_ALL_SOURCES.csv
  02_FISCAL_SURVEY_MASTER_LONG_SAFE_RESOLVED.csv
  03_COUNTRY_YEAR_CODE_RESOLUTION_AUDIT.csv
  04_CROSS_SOURCE_CONFLICT_ROWS.csv
  05_COUNTRY_YEAR_WIDE_BY_SOURCE.csv
  06_COUNTRY_YEAR_WIDE_SAFE_RESOLVED.csv
  07_INSTITUTION_COVERAGE_SUMMARY.csv
  08_VARIABLE_INSTITUTION_COVERAGE_MATRIX.csv
  09_WITHIN_SOURCE_KEY_AUDIT.csv
```

No cross-institution conflict is silently averaged. Conflicting source values are retained in the conflict audit.

## Useful commands

Run everything:

```bat
python RUN_ALL_FISCAL_PIPELINE.py
```

Run selected institutions only:

```bat
python RUN_ALL_FISCAL_PIPELINE.py --only OECD AFDB ECLAC EUROSTAT IDB
```

Run IDB only (also rebuilds the IDB mapping table):

```bat
python RUN_ALL_FISCAL_PIPELINE.py --only IDB
```

Force a fresh rerun:

```bat
python RUN_ALL_FISCAL_PIPELINE.py --force
```

Build only the IDB rule mapping package:

```bat
python idb_lmw_mapping_builder_final.py
```

Build IDB mapping plus mapped observations:

```bat
python idb_lmw_mapping_builder_final.py --with-data
```

## IDB reference

`reference/idb_lmw_annual_direct_survey_first_original.py` is the exact user-supplied reference script used as the basis for the integrated IDB rule-mapping workflow.
