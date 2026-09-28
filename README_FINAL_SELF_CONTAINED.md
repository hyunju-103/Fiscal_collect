# FINAL SELF-CONTAINED FISCAL PIPELINE V6

## One command

```bat
python RUN_FINAL_PIPELINE.py
```

This runs the six active institutions in this order:
OECD, ADB, AFDB, ECLAC, EUROSTAT, IDB. AMF is excluded.

At startup the package validates the frozen bundled registries. Expected Fiscal Survey code counts:
- OECD: 23
- ADB: 22
- AFDB: 9 (approved pre-reconcile registry; the 18-row candidate registry is intentionally not used)
- ECLAC: 28
- EUROSTAT: 15 (pre-reconcile reviewed registry)
- IDB: 9

The annual runner does NOT search sibling folders, does NOT rebuild mappings, and does NOT overwrite the bundled LOCKED registries.
Only production files already present under the CURRENT package's `annual_output/<year>/institution_data/` are auto-skipped. Use `--force` to re-download.

After all sources finish, the runner automatically executes `04_merge_final_fiscal_data.py --require-all`.

Final outputs are under:
`annual_output/<year>/final_merged/`

Main files:
- `01_FISCAL_SURVEY_MASTER_LONG_ALL_SOURCES.csv` — all source observations retained
- `02_FISCAL_SURVEY_MASTER_LONG_SAFE_RESOLVED.csv` — non-conflicting country-year-code observations
- `04_CROSS_SOURCE_CONFLICT_ROWS.csv` — source conflicts, never averaged silently
- `05_COUNTRY_YEAR_WIDE_BY_SOURCE.csv` — one variable per source
- `06_COUNTRY_YEAR_WIDE_SAFE_RESOLVED.csv` — analysis-ready safe wide file
- `07_INSTITUTION_COVERAGE_SUMMARY.csv`
- `08_VARIABLE_INSTITUTION_COVERAGE_MATRIX.csv`

## Registry policy
The user-provided mapping workbooks are included for documentation/review, but annual production uses the bundled frozen CSV registries as source of truth.

AFDB uses the 9-row approved `AFDB_LOCKED_pre_reconcile_20260923_141631.csv`, renamed to `AFDB_LOCKED.csv`.
EUROSTAT uses the 15-row pre-reconcile locked selection. Its direct API key is deterministically encoded from the locked transaction/unit as `freq=A|unit=<UNIT>|sector=S13|na_item=<TRANSACTION>`.

## Optional commands
- Full refresh even if current-package outputs exist: `python RUN_FINAL_PIPELINE.py --force`
- Run one source for debugging: `python RUN_FINAL_PIPELINE.py --only EUROSTAT --no-merge`
- Skip final merge: `python RUN_FINAL_PIPELINE.py --no-merge`


## ADB 503 resilience

The ADB collector first uses the normal LOCKED all-economy SDMX request. If KIDB returns HTTP 429/502/503/504 after the common retry cycle, the collector automatically retries the same LOCKED indicators in 10-economy chunks across the 50 ADB member economies. If an open-ended economy chunk is also rejected, only that chunk is retried with `endPeriod=2024`, which is the maximum currently documented by the KIDB API. Mapping and historical start year remain unchanged.


## IDB LOCKED annual override (V7)

IDB `idb_main.py` is intentionally left unchanged. Its internal mapping may label mapped Fiscal Survey candidates as REVIEW and therefore write a zero-row `03_idb_PRODUCTION_...csv`. In annual pipeline mode, the bundled `mapping_registry/IDB_LOCKED.csv` is the approval source of truth.

The runner now:
1. runs the unchanged IDB collector and refreshes the complete Annual Series history;
2. reads `02_idb_mapped_2007_latest_with_fiscal_survey_variables.csv`;
3. filters those mapped observations to bundled IDB LOCKED Fiscal Survey codes;
4. writes `data/idb_LOCKED_PRODUCTION_2007_latest.csv`;
5. continues even when some of the 9 LOCKED codes have no mapped IDB observations, recording them as MISSING in the coverage audit.

This prevents the collector's internal `PRODUCTION=0` judgment from overriding the separately reviewed annual LOCKED registry.
