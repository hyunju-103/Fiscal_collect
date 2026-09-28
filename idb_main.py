from __future__ import annotations

"""
IDB Latin Macro Watch -> Fiscal Survey mapper

Purpose
-------
1. Read ONLY the Latin Macro Watch package from IDB Open Data.
2. Prefer the "Latin Macro Watch - Annual Series" resource.
3. Download the FULL available annual history on every run.
4. Build a series inventory and map candidate series to the 64 Fiscal Survey variables.
5. Prefer nominal/current-price LCU series and broader government scope.
6. Export audit tables plus mapped long/wide country-year data.

This avoids scanning hundreds of unrelated IDB resources.
"""

import io
import json
import re
import time
import hashlib
from pathlib import Path
from datetime import datetime

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
IDB_API = "https://data.iadb.org/api/action"
PACKAGE_ID = "latin-macro-watch-dataset"
ANNUAL_RESOURCE_ID = "c2e50277-7e5a-4827-b6cd-5209fd7ba1ce"
ANNUAL_RESOURCE_NAME = "Latin Macro Watch - Annual Series"
SOURCE = "IDB"
SOURCE_DATASET = "IDB_LATIN_MACRO_WATCH"

# Always download all annual history. This is intentionally NOT used in the API
# query, because past observations may be revised by IDB/source institutions.
SURVEY_START_YEAR = 2007
DATASTORE_PAGE_SIZE = 10_000
REQUEST_TIMEOUT = 120
SLEEP_BETWEEN_CALLS = 0.05

OUTPUT_ROOT = Path(
    f"idb_lmw_fiscal_survey_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
)

# -----------------------------------------------------------------------------
# Canonical 64-variable Fiscal Survey registry used in the existing pipeline
# -----------------------------------------------------------------------------
FISCAL_MAPPING_RULES = [{'priority': 1,
  'category': 'Revenue',
  'code': 'GGREVTOTLCN',
  'variable': 'Total revenue',
  'include_any': ['government revenue',
                  'total revenue',
                  'fiscal revenue',
                  'government revenue',
                  'total fiscal revenue'],
  'include_all': [],
  'exclude_any': ['tax', 'grant'],
  'decision': 'EXACT_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Exact known KIDB candidate; preserve institutional-scope/unit checks.'},
 {'priority': 2,
  'category': 'Revenue',
  'code': 'GGREVTAXTCN',
  'variable': 'Tax revenue (including social contributions)',
  'include_any': ['tax revenue', 'government taxes'],
  'include_all': ['government'],
  'exclude_any': ['tax on', 'income tax', 'property tax', 'vat', 'excise', 'customs'],
  'decision': 'CONDITIONAL',
  'confidence': 'MEDIUM',
  'note': 'Only final if ADB definition includes social contributions.'},
 {'priority': 3,
  'category': 'Revenue',
  'code': 'GGREVDRCTCN',
  'variable': 'Direct taxes',
  'include_any': ['direct tax', 'direct taxes'],
  'include_all': [],
  'exclude_any': ['indirect'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only; verify source definition.'},
 {'priority': 4,
  'category': 'Revenue',
  'code': 'GGREVDRINCN',
  'variable': 'Income tax',
  'include_any': ['income tax', 'taxes on income'],
  'include_all': [],
  'exclude_any': ['personal', 'corporate', 'company'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Aggregate income-tax candidate.'},
 {'priority': 5,
  'category': 'Revenue',
  'code': 'GGREVDRPICN',
  'variable': 'Personal income tax',
  'include_any': ['personal income tax', 'individual income tax', 'personal tax'],
  'include_all': [],
  'exclude_any': ['corporate', 'company'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until definition/scope confirmed.'},
 {'priority': 6,
  'category': 'Revenue',
  'code': 'GGREVDRCICN',
  'variable': 'Corporate income tax',
  'include_any': ['corporate income tax', 'company income tax', 'corporate tax'],
  'include_all': [],
  'exclude_any': ['resource', 'commodity', 'personal'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until definition/scope confirmed.'},
 {'priority': 7,
  'category': 'Revenue',
  'code': 'GGREVDRCRCN',
  'variable': 'Corporate tax, resource (commodity) revenues',
  'include_any': ['resource corporate tax', 'commodity corporate tax', 'resource income tax'],
  'include_all': ['corporate'],
  'exclude_any': ['personal'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Resource-sector corporate tax only.'},
 {'priority': 8,
  'category': 'Revenue',
  'code': 'GGREVDROICN',
  'variable': 'Other (unallocable income taxes)',
  'include_any': ['other income tax', 'unallocable income tax'],
  'include_all': [],
  'exclude_any': ['personal', 'corporate'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 9,
  'category': 'Revenue',
  'code': 'GGREVDRPRCN',
  'variable': 'Taxes on property',
  'include_any': ['property tax', 'taxes on property'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until definition confirmed.'},
 {'priority': 10,
  'category': 'Revenue',
  'code': 'GGREVDROTCN',
  'variable': 'Other direct taxes',
  'include_any': ['other direct tax'],
  'include_all': [],
  'exclude_any': ['income', 'property'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 11,
  'category': 'Revenue',
  'code': 'GGREVIDGSCN',
  'variable': 'Taxes on goods and services',
  'include_any': ['taxes on goods and services', 'goods and services tax', 'tax on goods and services'],
  'include_all': [],
  'exclude_any': ['vat', 'value added', 'excise'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Aggregate GST-type tax candidate.'},
 {'priority': 12,
  'category': 'Revenue',
  'code': 'GGREVGNFSCN',
  'variable': 'General taxes on goods & services (incl. VAT/Sales)',
  'include_any': ['general taxes on goods and services', 'general sales tax', 'sales tax'],
  'include_all': [],
  'exclude_any': ['excise', 'specific'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Verify whether VAT/sales taxes are included.'},
 {'priority': 13,
  'category': 'Revenue',
  'code': 'GGREVVATTCN',
  'variable': 'VAT',
  'include_any': ['value added tax', 'value-added tax', 'vat'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until source definition confirmed.'},
 {'priority': 14,
  'category': 'Revenue',
  'code': 'GGREVEXSECN',
  'variable': 'Excise tax',
  'include_any': ['excise tax', 'excise taxes', 'excise duty', 'excise duties'],
  'include_all': [],
  'exclude_any': ['fuel', 'tobacco', 'alcohol', 'sugar'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Aggregate excise candidate.'},
 {'priority': 15,
  'category': 'Revenue',
  'code': 'GGREVEXFLCN',
  'variable': 'Excise tax on fuel',
  'include_any': ['fuel excise', 'excise on fuel', 'fuel tax', 'petroleum excise'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 16,
  'category': 'Revenue',
  'code': 'GGREVEXTBCN',
  'variable': 'Excise tax on tobacco',
  'include_any': ['tobacco excise', 'excise on tobacco', 'tobacco tax'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 17,
  'category': 'Revenue',
  'code': 'GGREVEXALCN',
  'variable': 'Excise tax on alcohol',
  'include_any': ['alcohol excise', 'excise on alcohol', 'alcohol tax'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 18,
  'category': 'Revenue',
  'code': 'GGREVEXSGCN',
  'variable': 'Excise tax on sugar-sweetened beverages',
  'include_any': ['sugar sweetened beverage tax', 'sugar-sweetened beverage tax', 'ssb tax', 'sugar tax'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 19,
  'category': 'Revenue',
  'code': 'GGREVEXOTCN',
  'variable': 'Other excise taxes',
  'include_any': ['other excise tax', 'other excise duties'],
  'include_all': [],
  'exclude_any': ['fuel', 'tobacco', 'alcohol', 'sugar'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 20,
  'category': 'Revenue',
  'code': 'GGREVGSOTCN',
  'variable': 'Other taxes on goods and services',
  'include_any': ['other taxes on goods and services', 'other goods and services taxes'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 21,
  'category': 'Revenue',
  'code': 'GGREVTRDECN',
  'variable': 'Taxes on international trade and transactions',
  'include_any': ['international trade tax',
                  'taxes on international trade',
                  'trade taxes',
                  'trade and transactions tax'],
  'include_all': [],
  'exclude_any': ['customs', 'import', 'export'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Aggregate trade-tax candidate.'},
 {'priority': 22,
  'category': 'Revenue',
  'code': 'GGREVCUSTCN',
  'variable': 'Customs & other import duties',
  'include_any': ['customs duty', 'customs duties', 'import duty', 'import duties', 'customs and import'],
  'include_all': [],
  'exclude_any': ['export'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 23,
  'category': 'Revenue',
  'code': 'GGREVEXPTCN',
  'variable': 'Taxes on exports',
  'include_any': ['export tax', 'export taxes', 'export duty', 'export duties'],
  'include_all': [],
  'exclude_any': ['import'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 24,
  'category': 'Revenue',
  'code': 'GGREVTROTCN',
  'variable': 'Other international trade taxes',
  'include_any': ['other international trade tax', 'other trade tax'],
  'include_all': [],
  'exclude_any': ['customs', 'import', 'export'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 25,
  'category': 'Revenue',
  'code': 'GGREVTOTRCN',
  'variable': 'Other resource (commodity) sector taxes',
  'include_any': ['resource tax', 'commodity tax', 'natural resource tax'],
  'include_all': [],
  'exclude_any': ['corporate'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 26,
  'category': 'Revenue',
  'code': 'GGREVSSOCCN',
  'variable': 'Social contributions',
  'include_any': ['social contribution', 'social contributions', 'social security contribution'],
  'include_all': [],
  'exclude_any': ['benefit'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 27,
  'category': 'Revenue',
  'code': 'GGREVTOTHCN',
  'variable': 'Other taxes',
  'include_any': ['other taxes', 'miscellaneous taxes'],
  'include_all': [],
  'exclude_any': ['direct', 'income', 'property', 'goods', 'excise', 'trade', 'resource'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'LOW',
  'note': 'Broad category; review required.'},
 {'priority': 28,
  'category': 'Revenue',
  'code': 'GGREVCOMMCN',
  'variable': 'Non-tax resource (commodity) revenues',
  'include_any': ['non-tax resource revenue',
                  'nontax resource revenue',
                  'commodity revenue',
                  'resource revenue'],
  'include_all': [],
  'exclude_any': ['tax'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Must be non-tax resource revenue.'},
 {'priority': 29,
  'category': 'Revenue',
  'code': 'GGREVGRNTCN',
  'variable': 'Grants revenue',
  'include_any': ['grants revenue', 'grant revenue', 'government grants received', 'grants received'],
  'include_all': [],
  'exclude_any': ['capital grant expenditure'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Revenue-side grants.'},
 {'priority': 30,
  'category': 'Revenue',
  'code': 'GGREVOTHRCN',
  'variable': 'Other revenue',
  'include_any': ['other revenue', 'miscellaneous revenue'],
  'include_all': [],
  'exclude_any': ['tax', 'grant', 'interest', 'resource'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'LOW',
  'note': 'Broad category; review required.'},
 {'priority': 31,
  'category': 'Revenue',
  'code': 'GGREVINTRCN',
  'variable': 'Interest revenue',
  'include_any': ['interest revenue', 'interest receipts', 'interest income'],
  'include_all': [],
  'exclude_any': ['expense', 'payment'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Revenue-side interest.'},
 {'priority': 32,
  'category': 'Expenditure',
  'code': 'GGEXPTOTLCN',
  'variable': 'Total expenditure',
  'include_any': ['government expenditure',
                  'total expenditure',
                  'total fiscal expenditure',
                  'government expenditure'],
  'include_all': [],
  'exclude_any': ['net lending'],
  'decision': 'EXACT_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Exact known KIDB candidate; preserve scope/unit checks.'},
 {'priority': 33,
  'category': 'Expenditure',
  'code': 'GGEXPCRNTCN',
  'variable': 'Current expenditure',
  'include_any': ['current expenditure', 'current expense'],
  'include_all': [],
  'exclude_any': ['capital'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 34,
  'category': 'Expenditure',
  'code': 'GGEXPWAGECN',
  'variable': 'Wages and compensation',
  'include_any': ['compensation of employees',
                  'employee compensation',
                  'wages and salaries',
                  'wages',
                  'salary'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 35,
  'category': 'Expenditure',
  'code': 'GGEXPGNFSCN',
  'variable': 'Use of goods and services',
  'include_any': ['use of goods and services',
                  'goods and services expense',
                  'government consumption goods and services'],
  'include_all': [],
  'exclude_any': ['tax'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Economic expenditure category.'},
 {'priority': 36,
  'category': 'Expenditure',
  'code': 'GGEXPINTPCN',
  'variable': 'Interest expense',
  'include_any': ['interest expense',
                  'interest expenditure',
                  'interest payment',
                  'interest payments',
                  'fiscal interests payments',
                  'interest payments',
                  'government interest payments'],
  'include_all': [],
  'exclude_any': ['revenue', 'receipt'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Aggregate interest expense.'},
 {'priority': 37,
  'category': 'Expenditure',
  'code': 'GGEXPINTECN',
  'variable': 'Interest payments on external public debt',
  'include_any': ['external debt interest', 'interest on external debt', 'external interest payment'],
  'include_all': [],
  'exclude_any': ['domestic'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'External public debt interest.'},
 {'priority': 38,
  'category': 'Expenditure',
  'code': 'GGEXPINTDCN',
  'variable': 'Interest payments on domestic public debt',
  'include_any': ['domestic debt interest', 'interest on domestic debt', 'domestic interest payment'],
  'include_all': [],
  'exclude_any': ['external'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Domestic public debt interest.'},
 {'priority': 39,
  'category': 'Expenditure',
  'code': 'GGEXPTRNSCN',
  'variable': 'Current transfers',
  'include_any': ['current transfers', 'current transfer'],
  'include_all': [],
  'exclude_any': ['capital'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 40,
  'category': 'Expenditure',
  'code': 'GGEXPTPNSCN',
  'variable': 'Social security benefits',
  'include_any': ['social security benefit', 'social benefits', 'social protection'],
  'include_all': [],
  'exclude_any': ['assistance'],
  'decision': 'PARTIAL_REVIEW',
  'confidence': 'LOW',
  'note': 'Functional social protection is not automatically equivalent to social-security benefits.'},
 {'priority': 41,
  'category': 'Expenditure',
  'code': 'GGEXPTSOCCN',
  'variable': 'Social assistance',
  'include_any': ['social assistance', 'social assistance benefits'],
  'include_all': [],
  'exclude_any': ['security contribution'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 42,
  'category': 'Expenditure',
  'code': 'GGEXPTOTSCN',
  'variable': 'Employment-related social benefits',
  'include_any': ['employment related social benefit', 'employment-related social benefit'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 43,
  'category': 'Expenditure',
  'code': 'GGEXPSUBSCN',
  'variable': 'Subsidies to businesses',
  'include_any': ['subsidies to business', 'business subsidies', 'subsidies', 'subsidy'],
  'include_all': [],
  'exclude_any': ['social'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Verify recipient/business scope.'},
 {'priority': 44,
  'category': 'Expenditure',
  'code': 'GGEXPTOTHCN',
  'variable': 'All other current transfers',
  'include_any': ['other current transfers', 'all other current transfers'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 45,
  'category': 'Expenditure',
  'code': 'GGEXPCROTCN',
  'variable': 'Other current expenditure',
  'include_any': ['other current expenditure', 'other current expense'],
  'include_all': [],
  'exclude_any': ['transfer'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 46,
  'category': 'Expenditure',
  'code': 'GGEXPCAPTCN',
  'variable': 'Capital expenditure',
  'include_any': ['capital expenditure', 'capital spending', 'development expenditure'],
  'include_all': [],
  'exclude_any': ['current'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 47,
  'category': 'Expenditure',
  'code': 'GGEXPKINVCN',
  'variable': 'Capital investment',
  'include_any': ['capital investment',
                  'government investment',
                  'public investment',
                  'gross fixed capital formation'],
  'include_all': [],
  'exclude_any': ['private'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Verify whether definition matches Fiscal Survey capital investment.'},
 {'priority': 48,
  'category': 'Expenditure',
  'code': 'GGEXPKCFKCN',
  'variable': 'Consumption of fixed capital',
  'include_any': ['consumption of fixed capital', 'depreciation'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 49,
  'category': 'Expenditure',
  'code': 'GGEXPKTRNCN',
  'variable': 'Other capital expenditure',
  'include_any': ['other capital expenditure', 'capital transfers', 'other capital spending'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 50,
  'category': 'Expenditure',
  'code': 'GGEXPOTHRCN',
  'variable': 'Other expenditure',
  'include_any': ['other expenditure', 'miscellaneous expenditure'],
  'include_all': [],
  'exclude_any': ['current', 'capital'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'LOW',
  'note': 'Broad category; review required.'},
 {'priority': 51,
  'category': 'Balance',
  'code': 'GGBALOVRACN',
  'variable': 'Overall fiscal balance (accrual / above-the-line)',
  'include_any': ['net lending net borrowing',
                  'net lending/net borrowing',
                  'overall fiscal balance',
                  'fiscal balance',
                  'overall fiscal balance',
                  'overall balance'],
  'include_all': [],
  'exclude_any': ['cash'],
  'decision': 'EXACT_CANDIDATE_BASIS_CHECK',
  'confidence': 'HIGH',
  'note': 'Known closest concept; verify accounting basis and institutional coverage.'},
 {'priority': 52,
  'category': 'Balance',
  'code': 'GGBALOVRLCN',
  'variable': 'Overall fiscal balance (cash / below-the-line)',
  'include_any': ['cash balance', 'cash fiscal balance', 'overall balance cash'],
  'include_all': [],
  'exclude_any': ['accrual', 'net lending'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Do not use generic net lending/borrowing as cash balance.'},
 {'priority': 53,
  'category': 'Balance',
  'code': 'GGBALDISCCN',
  'variable': 'Discrepancy between cash and commitment balance',
  'include_any': ['cash commitment discrepancy', 'statistical discrepancy', 'fiscal discrepancy'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Review definition carefully.'},
 {'priority': 54,
  'category': 'Financing',
  'code': 'GGFINEXTLCN',
  'variable': 'Net external financing',
  'include_any': ['net external financing', 'external financing net'],
  'include_all': [],
  'exclude_any': ['domestic'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Financing flow, not debt stock.'},
 {'priority': 55,
  'category': 'Financing',
  'code': 'GGFINEDSBCN',
  'variable': 'External debt disbursements',
  'include_any': ['external debt disbursement', 'external disbursement', 'foreign loan disbursement'],
  'include_all': [],
  'exclude_any': ['domestic'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Flow variable.'},
 {'priority': 56,
  'category': 'Financing',
  'code': 'GGFINEAMTCN',
  'variable': 'Amortization of external debt',
  'include_any': ['external debt amortization', 'external amortization', 'external debt repayment'],
  'include_all': [],
  'exclude_any': ['domestic', 'interest'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Principal repayment flow.'},
 {'priority': 57,
  'category': 'Financing',
  'code': 'GGFINEOTHCN',
  'variable': 'Other external financing',
  'include_any': ['other external financing'],
  'include_all': [],
  'exclude_any': ['domestic'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 58,
  'category': 'Financing',
  'code': 'GGFINDOMTCN',
  'variable': 'Net domestic financing',
  'include_any': ['net domestic financing', 'domestic financing net'],
  'include_all': [],
  'exclude_any': ['external'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Financing flow.'},
 {'priority': 59,
  'category': 'Financing',
  'code': 'GGFINDDSBCN',
  'variable': 'Domestic debt disbursements',
  'include_any': ['domestic debt disbursement', 'domestic borrowing disbursement'],
  'include_all': [],
  'exclude_any': ['external'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Flow variable.'},
 {'priority': 60,
  'category': 'Financing',
  'code': 'GGFINDAMTCN',
  'variable': 'Amortization of domestic debt',
  'include_any': ['domestic debt amortization', 'domestic amortization', 'domestic debt repayment'],
  'include_all': [],
  'exclude_any': ['external', 'interest'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Principal repayment flow.'},
 {'priority': 61,
  'category': 'Financing',
  'code': 'GGDBTDOOTCN',
  'variable': 'Other domestic financing',
  'include_any': ['other domestic financing'],
  'include_all': [],
  'exclude_any': ['external'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 62,
  'category': 'Debt',
  'code': 'GGDBTTOTLCN',
  'variable': 'Total gross debt',
  'include_any': ['total gross debt',
                  'gross public debt',
                  'general government gross debt',
                  'government gross debt',
                  'public debt',
                  'total public debt',
                  'public debt total',
                  'gross public debt'],
  'include_all': [],
  'exclude_any': ['external debt', 'domestic debt', 'private'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Debt stock; verify general-government/public-sector scope.'},
 {'priority': 63,
  'category': 'Debt',
  'code': 'GGDBTEXTLCN',
  'variable': 'External gross debt',
  'include_any': ['external gross debt',
                  'government external debt',
                  'public external debt',
                  'external public debt',
                  'external public debt',
                  'public external debt'],
  'include_all': [],
  'exclude_any': ['private', 'domestic'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Debt stock; verify government scope.'},
 {'priority': 64,
  'category': 'Debt',
  'code': 'GGDBTDOMTCN',
  'variable': 'Domestic gross debt',
  'include_any': ['domestic gross debt',
                  'government domestic debt',
                  'public domestic debt',
                  'domestic public debt'],
  'include_all': [],
  'exclude_any': ['external', 'private'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Debt stock; verify government scope.'}]

VALID_FISCAL_CODES = {r["code"] for r in FISCAL_MAPPING_RULES}
FISCAL_META = {
    r["code"]: {
        "priority": r["priority"],
        "category": r["category"],
        "variable": r["variable"],
    }
    for r in FISCAL_MAPPING_RULES
}

HARD_FALSE_POSITIVES = (
    "bank lending rate",
    "gross fixed capital formation",
    "net exports",
    "private sector",
    "household",
)

# Source-specific high-confidence anchors. Generic rule matches remain review
# candidates; these anchors can be production-approved only when the unit/basis
# is compatible with the Fiscal Survey level concept.
IDB_EXACT = [
    ("fiscal revenue", "GGREVTOTLCN", "Total revenue"),
    ("total fiscal expenditure", "GGEXPTOTLCN", "Total expenditure"),
    ("fiscal interest payments", "GGEXPINTPCN", "Interest expense"),
    ("fiscal interests payments", "GGEXPINTPCN", "Interest expense"),
    ("overall fiscal balance", "GGBALOVRACN", "Overall fiscal balance (accrual / above-the-line)"),
    ("primary balance", "GGBALPRIMCN", "Primary balance"),
    ("capital fiscal expenditures", "GGEXPCAPTCN", "Capital expenditure"),
    ("current fiscal expenditures", "GGEXPCRNTCN", "Current expenditure"),
    ("total public debt", "GGDBTTOTLCN", "Total gross debt"),
    ("external public debt", "GGDBTEXTLCN", "External gross debt"),
    ("domestic public debt", "GGDBTDOMTCN", "Domestic gross debt"),
]

ISO3 = {
    "argentina":"ARG", "bahamas":"BHS", "the bahamas":"BHS", "barbados":"BRB",
    "belize":"BLZ", "bolivia":"BOL", "bolivia (plurinational state of)":"BOL",
    "brazil":"BRA", "chile":"CHL", "colombia":"COL", "costa rica":"CRI",
    "dominican republic":"DOM", "ecuador":"ECU", "el salvador":"SLV",
    "guatemala":"GTM", "guyana":"GUY", "haiti":"HTI", "honduras":"HND",
    "jamaica":"JAM", "mexico":"MEX", "nicaragua":"NIC", "panama":"PAN",
    "paraguay":"PRY", "peru":"PER", "suriname":"SUR",
    "trinidad and tobago":"TTO", "uruguay":"URY", "venezuela":"VEN",
    "venezuela, rb":"VEN", "venezuela (bolivarian republic of)":"VEN",
}


def norm(x) -> str:
    s = str(x or "").lower()
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def txt(x) -> str:
    if isinstance(x, dict):
        for k in ("en", "en-US", "es", "pt", "fr"):
            if x.get(k):
                return str(x[k])
        return " ".join(str(v) for v in x.values())
    return str(x or "")


def make_session() -> requests.Session:
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    s = requests.Session()
    s.headers.update({"User-Agent": "IDB-LMW-Fiscal-Survey/2026-09"})
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4))
    return s


SESSION = make_session()


def api_json(action: str, params: dict) -> dict:
    url = f"{IDB_API}/{action}"
    r = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    js = r.json()
    if not js.get("success"):
        raise RuntimeError(f"IDB API action failed: {action} params={params}")
    return js["result"]


def package_show(package_id: str = PACKAGE_ID) -> dict:
    return api_json("package_show", {"id": package_id})


def resource_name(r: dict) -> str:
    return txt(r.get("name") or r.get("title"))


def resource_format(r: dict) -> str:
    return txt(r.get("format")).upper()


def direct_url(r: dict) -> str | None:
    for k in ("url", "download_url", "resource_url"):
        if r.get(k):
            return str(r[k])
    return None


def choose_annual_resource(pkg: dict) -> dict:
    resources = list(pkg.get("resources") or [])
    if not resources:
        raise RuntimeError("Latin Macro Watch package contains no resources.")

    def score(r: dict) -> tuple:
        n = norm(resource_name(r))
        s = 0
        if "latin macro watch" in n and "annual series" in n:
            s += 10_000
        elif "annual series" in n:
            s += 8_000
        elif n in {"latin macro watch - dataset", "latin macro watch dataset"}:
            s += 5_000
        elif "latin macro watch" in n and "dataset" in n:
            s += 4_000
        if r.get("datastore_active"):
            s += 500
        if resource_format(r) == "CSV":
            s += 100
        if direct_url(r):
            s += 20
        return (s, n)

    ranked = sorted(resources, key=score, reverse=True)
    chosen = ranked[0]
    if score(chosen)[0] < 4_000:
        candidates = [(resource_name(r), resource_format(r), bool(r.get("datastore_active"))) for r in ranked[:20]]
        raise RuntimeError(f"Could not identify Latin Macro Watch annual/master resource. Top resources={candidates}")
    return chosen


def fetch_datastore(resource: dict) -> pd.DataFrame:
    rid = resource.get("id") or resource.get("resource_id")
    if not rid:
        raise RuntimeError("Datastore resource has no resource id.")

    rows = []
    offset = 0
    total = None
    while True:
        result = api_json(
            "datastore_search",
            {"resource_id": rid, "limit": DATASTORE_PAGE_SIZE, "offset": offset},
        )
        if total is None:
            total = int(result.get("total") or 0)
            print(f"  Datastore total rows: {total:,}")
        recs = result.get("records") or []
        if not recs:
            break
        rows.extend(recs)
        offset += len(recs)
        if offset == len(recs) or offset % 50_000 == 0 or (total and offset >= total):
            print(f"    downloaded {offset:,}/{total:,}")
        if total and offset >= total:
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    return pd.DataFrame(rows)


def fetch_direct(resource: dict) -> pd.DataFrame:
    url = direct_url(resource)
    if not url:
        raise RuntimeError("Selected resource has no datastore and no direct URL.")
    r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    content = r.content
    if not content:
        raise RuntimeError("Direct resource returned zero bytes.")
    fmt = resource_format(resource)
    ctype = (r.headers.get("content-type") or "").lower()
    if fmt == "CSV" or "csv" in ctype or url.lower().split("?")[0].endswith(".csv"):
        return pd.read_csv(io.BytesIO(content), low_memory=False)
    if fmt in {"XLS", "XLSX"} or "excel" in ctype:
        return pd.read_excel(io.BytesIO(content))
    try:
        return pd.read_csv(io.BytesIO(content), low_memory=False)
    except Exception as e:
        raise RuntimeError(f"Unsupported direct resource format: {fmt}, {ctype}") from e


def fetch_resource(resource: dict) -> pd.DataFrame:
    if resource.get("datastore_active"):
        return fetch_datastore(resource)
    return fetch_direct(resource)


def first_col(df: pd.DataFrame, *names: str) -> str | None:
    lookup = {str(c).lower(): c for c in df.columns}
    for n in names:
        if n in df.columns:
            return n
        if n.lower() in lookup:
            return lookup[n.lower()]
    return None


def normalize_lmw(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    rename = {}
    aliases = {
        "Country": ("Country", "country", "Economy", "economy"),
        "Frequency": ("Frequency", "frequency", "freq"),
        "Year": ("Year", "year", "TIME_PERIOD", "time"),
        "Date": ("Date", "date"),
        "Value": ("Value", "value", "OBS_VALUE", "obs_value"),
        "Indicator": ("Indicator", "indicator", "Series", "series"),
        "Unit": ("Unit", "unit", "UNIT"),
        "Transformation": ("Transformation", "transformation", "Transform"),
        "Area": ("Area", "area"),
        "Topic": ("Topic", "topic"),
        "Definition": ("Definition", "definition"),
        "Source": ("Source", "source"),
        "Notes": ("Notes", "notes"),
        "Updated": ("Updated", "updated", "Last Updated"),
        "lmwuuid": ("lmwuuid", "LMWUUID", "uuid"),
    }
    for canonical, opts in aliases.items():
        c = first_col(df, *opts)
        if c and c != canonical:
            rename[c] = canonical
    df = df.rename(columns=rename)

    required = ["Country", "Year", "Value", "Indicator"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"LMW schema missing required columns {missing}. Columns={list(df.columns)}")

    # The selected Annual Series should already be annual. If the master dataset
    # is selected as fallback, keep only annual observations locally.
    if "Frequency" in df.columns:
        f = df["Frequency"].astype(str).str.lower().str.strip()
        annual = f.str.startswith("annual") | f.eq("a") | f.eq("yearly")
        if annual.any():
            df = df.loc[annual].copy()

    df["Year"] = pd.to_numeric(df["Year"], errors="coerce").astype("Int64")
    df["Value"] = pd.to_numeric(df["Value"], errors="coerce")
    df = df[df["Year"].notna() & df["Value"].notna() & df["Country"].notna()].copy()

    for c in ["Frequency", "Unit", "Transformation", "Area", "Topic", "Definition", "Source", "Notes", "Updated", "lmwuuid"]:
        if c not in df.columns:
            df[c] = ""

    # Keep the latest record if the source contains duplicate observations with an
    # Updated timestamp. Do not aggregate values.
    key = ["Country", "Year", "Indicator", "Unit", "Transformation", "Frequency"]
    if "Updated" in df.columns:
        df["_updated_dt"] = pd.to_datetime(df["Updated"], errors="coerce")
        df = df.sort_values(key + ["_updated_dt"], na_position="first")
    df = df.drop_duplicates(key, keep="last").drop(columns=["_updated_dt"], errors="ignore")

    return df.reset_index(drop=True)


def basis_and_scope(indicator: str, unit: str, transformation: str) -> tuple[int, str, str]:
    text = norm(f"{indicator} | {unit} | {transformation}")
    score = 0
    basis = "OTHER"

    # Basis / unit preference. Fiscal Survey *CN variables are level concepts,
    # so nominal/current LCU is preferred over ratios, USD, or constant prices.
    if "constant price" in text or "real price" in text:
        score -= 250
        basis = "CONSTANT_LCU" if "lcu" in text else "CONSTANT_OTHER"
    elif ("current price" in text and "lcu" in text) or ("million" in text and "lcu" in text):
        score += 350
        basis = "CURRENT_LCU"
    elif "lcu" in text or "local currency" in text:
        score += 300
        basis = "LCU"
    elif "% of gdp" in text or "percent of gdp" in text:
        score += 60
        basis = "PCT_GDP"
    elif "usd" in text or "us$" in text or "us dollar" in text:
        score += 20
        basis = "CURRENT_USD"

    # Transformations that are not level values should not win a Fiscal Survey
    # nominal-level mapping even when the indicator name matches.
    if any(x in text for x in ["yoy", "year on year", "growth", "index", "contribution"]):
        score -= 300
        basis = "TRANSFORMED_NONLEVEL"

    if "general government" in text:
        score += 180
        scope = "GENERAL_GOVERNMENT"
    elif "central government" in text:
        score += 120
        scope = "CENTRAL_GOVERNMENT"
    elif "non financial public sector" in text or "non-financial public sector" in text:
        score += 80
        scope = "NFPS"
    else:
        scope = "UNSPECIFIED"

    if "last 4 quarters" in text or "last 12 months" in text:
        score -= 80

    return score, basis, scope


def exact_match(indicator: str) -> tuple[str | None, str | None, str | None]:
    n = norm(indicator)
    # Longer/more specific phrases first.
    for phrase, code, var in sorted(IDB_EXACT, key=lambda x: len(x[0]), reverse=True):
        if norm(phrase) in n:
            return code, var, phrase
    return None, None, None


def generic_matches(indicator: str) -> list[dict]:
    t = norm(indicator)
    if any(x in t for x in HARD_FALSE_POSITIVES):
        return []
    out = []
    for rule in FISCAL_MAPPING_RULES:
        excludes = [norm(x) for x in rule.get("exclude_any", []) if norm(x)]
        if excludes and any(x in t for x in excludes):
            continue
        req = [norm(x) for x in rule.get("include_all", []) if norm(x)]
        if req and not all(x in t for x in req):
            continue
        hits = [x for x in rule.get("include_any", []) if norm(x) and norm(x) in t]
        if not hits:
            continue
        text_score = 50 + max(len(norm(x).split()) for x in hits) * 20 + len(hits) * 5
        out.append({
            "fiscal_survey_code": rule["code"],
            "fiscal_survey_variable": rule["variable"],
            "mapping_status": rule.get("decision", "AUTO_CANDIDATE"),
            "mapping_confidence": rule.get("confidence", "MEDIUM"),
            "matched_phrases": " | ".join(hits),
            "text_score": text_score,
            "mapping_method": "IDB_LMW_RULE_CANDIDATE",
            "production_approved": False,
        })
    return out


def make_series_inventory(df: pd.DataFrame) -> pd.DataFrame:
    series_cols = ["Indicator", "Unit", "Transformation", "Frequency", "Area", "Topic"]
    for c in series_cols:
        if c not in df.columns:
            df[c] = ""

    def first_nonempty(s):
        for v in s:
            if pd.notna(v) and str(v).strip():
                return str(v)
        return ""

    g = (
        df.groupby(series_cols, dropna=False)
        .agg(
            Rows=("Value", "size"),
            Countries=("Country", "nunique"),
            Min_Year=("Year", "min"),
            Max_Year=("Year", "max"),
            Definition=("Definition", first_nonempty),
            Source=("Source", first_nonempty),
            Updated=("Updated", first_nonempty),
        )
        .reset_index()
    )

    def sid(row):
        raw = "||".join(str(row.get(c, "")) for c in series_cols)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    g["series_id"] = g.apply(sid, axis=1)
    return g


def build_mapping_candidates(inv: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, s in inv.iterrows():
        indicator = str(s.get("Indicator", ""))
        unit = str(s.get("Unit", ""))
        transformation = str(s.get("Transformation", ""))
        basis_score, basis, scope = basis_and_scope(indicator, unit, transformation)

        code, var, phrase = exact_match(indicator)
        if code and code in VALID_FISCAL_CODES:
            definition_text = norm(str(s.get("Definition", "")))
            concept_ok = True
            # Fiscal Survey GG concepts should not be silently filled with a narrower
            # central-government/NFPS series. Keep those as REVIEW candidates.
            scope_ok = scope == "GENERAL_GOVERNMENT"
            # Overall balance in the Survey is explicitly accrual/above-the-line;
            # a generic "overall fiscal balance" label is not enough to prove basis.
            if code == "GGBALOVRACN":
                concept_ok = any(k in norm(indicator + " " + definition_text) for k in [
                    "accrual", "net lending", "net borrowing"
                ])
            approved_basis = basis in {"CURRENT_LCU", "LCU"} and scope_ok and concept_ok
            rows.append({
                **s.to_dict(),
                "fiscal_survey_code": code,
                "fiscal_survey_variable": var,
                "mapping_status": "SOURCE_SPECIFIC_EXACT",
                "mapping_confidence": "HIGH",
                "mapping_method": "IDB_LMW_SOURCE_EXACT",
                "matched_phrases": phrase,
                "basis": basis,
                "government_scope": scope,
                "basis_score": basis_score,
                "text_score": 500,
                "mapping_score": 1000 + 500 + basis_score + min(int(s["Countries"]), 50),
                "production_approved": bool(approved_basis),
            })

        for m in generic_matches(indicator):
            rows.append({
                **s.to_dict(),
                **m,
                "basis": basis,
                "government_scope": scope,
                "basis_score": basis_score,
                "mapping_score": m["text_score"] + basis_score + min(int(s["Countries"]), 50),
            })

    cand = pd.DataFrame(rows)
    if cand.empty:
        return cand
    cand = cand.sort_values(
        ["fiscal_survey_code", "mapping_score", "Countries", "Rows"],
        ascending=[True, False, False, False],
    ).reset_index(drop=True)
    return cand


def select_best_mapping(cand: pd.DataFrame) -> pd.DataFrame:
    if cand.empty:
        return cand.copy()
    # Prefer production-approved source-specific matches. If none exists for a
    # code, keep the strongest candidate for audit/review rather than silently
    # inventing a production mapping.
    c = cand.copy()
    c["_approved_rank"] = c["production_approved"].astype(int)
    best = (
        c.sort_values(
            ["fiscal_survey_code", "_approved_rank", "mapping_score", "Countries", "Rows"],
            ascending=[True, False, False, False, False],
        )
        .drop_duplicates("fiscal_survey_code", keep="first")
        .drop(columns=["_approved_rank"])
        .reset_index(drop=True)
    )
    return best


def attach_series_id(df: pd.DataFrame) -> pd.DataFrame:
    series_cols = ["Indicator", "Unit", "Transformation", "Frequency", "Area", "Topic"]
    x = df.copy()
    def sid(row):
        raw = "||".join(str(row.get(c, "")) for c in series_cols)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    x["series_id"] = x.apply(sid, axis=1)
    return x


def make_mapped_data(df: pd.DataFrame, selected: pd.DataFrame) -> pd.DataFrame:
    """
    Attach the selected Fiscal Survey mapping to actual IDB observations.

    Important: the Fiscal Survey fields are placed FIRST so each IDB value can be
    read as: "this IDB observation fills this Fiscal Survey variable".
    The merge uses the six structural series columns directly, avoiding a slow
    row-by-row SHA1 calculation over the full observation table.
    """
    if selected.empty:
        return pd.DataFrame()

    series_cols = ["Indicator", "Unit", "Transformation", "Frequency", "Area", "Topic"]
    selected_cols = series_cols + [
        "series_id", "fiscal_survey_code", "fiscal_survey_variable",
        "mapping_status", "mapping_confidence", "mapping_method",
        "mapping_score", "production_approved", "basis", "government_scope",
        "matched_phrases",
    ]
    selected_cols = [c for c in selected_cols if c in selected.columns]

    x = df.merge(
        selected[selected_cols],
        on=[c for c in series_cols if c in df.columns and c in selected.columns],
        how="inner",
        validate="many_to_many",
    )

    x["Fiscal Survey Priority"] = x["fiscal_survey_code"].map(
        lambda z: FISCAL_META.get(z, {}).get("priority")
    )
    x["Fiscal Survey Category"] = x["fiscal_survey_code"].map(
        lambda z: FISCAL_META.get(z, {}).get("category", "")
    )
    x["Fiscal Survey Code"] = x["fiscal_survey_code"]
    x["Fiscal Survey Variable"] = x["fiscal_survey_variable"]
    x["Final Mapping Status"] = "REVIEW"
    x.loc[x["production_approved"].eq(True), "Final Mapping Status"] = "PRODUCTION"

    x["country_original"] = x["Country"].astype(str)
    x["country_code"] = x["country_original"].map(lambda z: ISO3.get(norm(z), ""))
    x["year"] = pd.to_numeric(x["Year"], errors="coerce").astype("Int64")
    x["value_original"] = pd.to_numeric(x["Value"], errors="coerce")
    x["unit_original"] = x["Unit"].astype(str)
    x["indicator_original"] = x["Indicator"].astype(str)
    x["source_dataset"] = SOURCE_DATASET
    x["source"] = SOURCE
    x["wbfd_mnemonic"] = x["Fiscal Survey Code"]
    x["wbfd_variable"] = x["Fiscal Survey Variable"]

    # Survey-first column order, then the actual mapped IDB observation and metadata.
    out_cols = [
        "Fiscal Survey Priority", "Fiscal Survey Category", "Fiscal Survey Code",
        "Fiscal Survey Variable", "Final Mapping Status",
        "country_code", "country_original", "year", "value_original",
        "indicator_original", "unit_original", "Transformation", "Frequency",
        "Area", "Topic", "Definition", "Source", "Notes", "Updated", "lmwuuid",
        "mapping_status", "mapping_confidence", "mapping_method", "mapping_score",
        "production_approved", "basis", "government_scope", "matched_phrases",
        "series_id", "source", "source_dataset", "wbfd_mnemonic", "wbfd_variable",
    ]
    out_cols = [c for c in out_cols if c in x.columns]
    x = x[out_cols].copy()
    x = x[x["year"].notna() & x["value_original"].notna()].copy()
    x = x.drop_duplicates()
    return x.sort_values(
        ["Fiscal Survey Priority", "country_original", "year"]
    ).reset_index(drop=True)


def build_survey_first_mapping(master: pd.DataFrame, selected: pd.DataFrame) -> pd.DataFrame:
    """One row per existing Fiscal Survey variable, followed by its selected IDB series."""
    full = master.copy()
    if selected.empty:
        full["Final Mapping Status"] = "GAP"
        return full

    s = selected.rename(columns={
        "fiscal_survey_code": "Fiscal Survey Code",
        "Indicator": "Mapped IDB Indicator",
        "Definition": "IDB Definition",
        "Topic": "IDB Topic",
        "Area": "IDB Area",
        "Unit": "IDB Unit",
        "Transformation": "IDB Transformation",
        "Source": "IDB Source",
        "mapping_status": "Runtime Status",
        "mapping_confidence": "Mapping Confidence",
        "mapping_method": "Mapping Method",
        "mapping_score": "Mapping Score",
        "production_approved": "Production Approved",
        "basis": "Basis",
        "government_scope": "Government Scope",
        "Countries": "Countries",
        "Rows": "Rows",
        "Min_Year": "Min Year",
        "Max_Year": "Max Year",
        "matched_phrases": "Matched Phrases",
    })
    keep = [
        "Fiscal Survey Code", "Mapped IDB Indicator", "IDB Definition", "IDB Topic",
        "IDB Area", "IDB Unit", "IDB Transformation", "IDB Source",
        "Runtime Status", "Mapping Confidence", "Mapping Method", "Mapping Score",
        "Production Approved", "Basis", "Government Scope", "Countries", "Rows",
        "Min Year", "Max Year", "Matched Phrases", "series_id",
    ]
    full = full.merge(s[[c for c in keep if c in s.columns]], on="Fiscal Survey Code", how="left")
    full["Final Mapping Status"] = "GAP"
    full.loc[full["Mapped IDB Indicator"].notna(), "Final Mapping Status"] = "REVIEW"
    if "Production Approved" in full.columns:
        full.loc[full["Production Approved"].eq(True), "Final Mapping Status"] = "PRODUCTION"

    first = [
        "Priority", "Category", "Fiscal Survey Code", "Fiscal Survey Variable",
        "Final Mapping Status", "Mapped IDB Indicator", "IDB Unit",
        "Government Scope", "Mapping Method", "Mapping Confidence", "Mapping Score",
        "Production Approved", "IDB Definition", "IDB Topic", "IDB Area",
        "IDB Transformation", "IDB Source", "Countries", "Rows", "Min Year",
        "Max Year", "Matched Phrases",
    ]
    rest = [c for c in full.columns if c not in first]
    return full[[c for c in first if c in full.columns] + rest]

def build_wide(mapped: pd.DataFrame, start_year: int = SURVEY_START_YEAR) -> pd.DataFrame:
    if mapped.empty:
        return pd.DataFrame()
    x = mapped[
        mapped["production_approved"].eq(True)
        & (pd.to_numeric(mapped["year"], errors="coerce") >= start_year)
    ].copy()
    if x.empty:
        return pd.DataFrame()

    # Exact duplicate rows are removed; unresolved multi-valued collisions are
    # reported separately and excluded from the wide pivot rather than averaged.
    x = x.drop_duplicates(["country_code", "country_original", "year", "wbfd_mnemonic", "value_original"])
    dup = x.duplicated(["country_code", "country_original", "year", "wbfd_mnemonic"], keep=False)
    if dup.any():
        bad_keys = x.loc[dup, ["country_code", "country_original", "year", "wbfd_mnemonic"]].drop_duplicates()
        print(f"  WARNING: {len(bad_keys):,} country-year-code collisions excluded from wide output.")
        bad_index = set(map(tuple, bad_keys.astype(str).to_records(index=False)))
        keep = []
        for _, r in x.iterrows():
            k = tuple(map(str, [r["country_code"], r["country_original"], r["year"], r["wbfd_mnemonic"]]))
            keep.append(k not in bad_index)
        x = x.loc[keep]

    wide = x.pivot(
        index=["country_code", "country_original", "year"],
        columns="wbfd_mnemonic",
        values="value_original",
    ).reset_index()
    wide.columns.name = None
    return wide.sort_values(["country_original", "year"]).reset_index(drop=True)


def export_mapping_workbook(path: Path, pkg: dict, resource: dict, inv: pd.DataFrame,
                            cand: pd.DataFrame, selected: pd.DataFrame, mapped: pd.DataFrame):
    rules = pd.DataFrame(FISCAL_MAPPING_RULES).rename(columns={
        "priority":"Priority", "category":"Category", "code":"Fiscal Survey Code",
        "variable":"Fiscal Survey Variable", "decision":"Discovery Rule Decision",
        "confidence":"Discovery Rule Confidence", "note":"Discovery Rule Note",
    })
    master = rules[[
        "Priority", "Category", "Fiscal Survey Code", "Fiscal Survey Variable",
        "Discovery Rule Decision", "Discovery Rule Confidence", "Discovery Rule Note"
    ]].copy()

    full = build_survey_first_mapping(master, selected)
    gaps = full[full["Final Mapping Status"].eq("GAP")].copy()
    mapped_only = full[full["Final Mapping Status"].ne("GAP")].copy()
    prod_map = full[full["Final Mapping Status"].eq("PRODUCTION")].copy()
    production_codes = int((full["Final Mapping Status"] == "PRODUCTION").sum())

    summary = pd.DataFrame([
        {"Metric":"IDB package", "Value": txt(pkg.get("title") or pkg.get("name"))},
        {"Metric":"Package modified", "Value": pkg.get("metadata_modified", "")},
        {"Metric":"Primary annual resource", "Value": resource_name(resource)},
        {"Metric":"Annual resource id", "Value": resource.get("id", "")},
        {"Metric":"Datastore active", "Value": bool(resource.get("datastore_active"))},
        {"Metric":"LMW annual series discovered", "Value": len(inv)},
        {"Metric":"Mapping candidate links", "Value": len(cand)},
        {"Metric":"Fiscal Survey variables", "Value": len(FISCAL_MAPPING_RULES)},
        {"Metric":"Production-approved Fiscal Survey codes", "Value": production_codes},
        {"Metric":"Review mappings", "Value": int((full["Final Mapping Status"] == "REVIEW").sum())},
        {"Metric":"Remaining gaps", "Value": int((full["Final Mapping Status"] == "GAP").sum())},
        {"Metric":"Rule", "Value":"Survey-first output. Exact IDB source matches with nominal/current LCU are PRODUCTION; other plausible links remain REVIEW; unmatched Survey variables are GAP."},
    ])

    mapped_2007 = mapped[pd.to_numeric(mapped["year"], errors="coerce") >= SURVEY_START_YEAR].copy() if not mapped.empty else mapped
    production_data = mapped_2007[mapped_2007["Final Mapping Status"].eq("PRODUCTION")].copy() if not mapped_2007.empty else mapped_2007

    with pd.ExcelWriter(path, engine="openpyxl") as w:
        summary.to_excel(w, sheet_name="00 Summary", index=False)
        full.to_excel(w, sheet_name="01 Fiscal Survey Mapping", index=False)
        mapped_only.to_excel(w, sheet_name="02 Mapped Variables", index=False)
        production_data.to_excel(w, sheet_name="03 Mapped IDB Data", index=False)
        gaps.to_excel(w, sheet_name="04 Survey GAP", index=False)
        prod_map.to_excel(w, sheet_name="05 Production Mapping", index=False)
        cand.to_excel(w, sheet_name="06 All Candidates", index=False)
        inv.to_excel(w, sheet_name="07 IDB Series Inventory", index=False)
        master.to_excel(w, sheet_name="08 Fiscal Survey 64", index=False)

        from copy import copy
        for ws in w.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                f = copy(cell.font); f.bold = True; cell.font = f
            for col in ws.columns:
                letter = col[0].column_letter
                width = max((len(str(c.value)) if c.value is not None else 0 for c in col[:200]), default=10)
                ws.column_dimensions[letter].width = min(max(width + 2, 10), 55)


def direct_annual_resource() -> dict:
    return {
        "id": ANNUAL_RESOURCE_ID,
        "name": ANNUAL_RESOURCE_NAME,
        "title": ANNUAL_RESOURCE_NAME,
        "format": "CSV",
        "datastore_active": True,
    }


def fetch_annual_with_fallback(pkg: dict) -> tuple[dict, pd.DataFrame, str]:
    """Use the known Annual Series resource directly; rediscover only if IDB changes it."""
    primary = direct_annual_resource()
    try:
        raw = fetch_datastore(primary)
        return primary, raw, "DIRECT_FIXED_ANNUAL_RESOURCE"
    except Exception as e:
        print(f"   Direct annual resource failed: {e}")
        print("   Falling back to Latin Macro Watch package discovery ...")
        fallback = choose_annual_resource(pkg)
        raw = fetch_resource(fallback)
        return fallback, raw, "PACKAGE_DISCOVERY_FALLBACK"

def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    data_dir = OUTPUT_ROOT / "data"
    meta_dir = OUTPUT_ROOT / "metadata"
    data_dir.mkdir(exist_ok=True)
    meta_dir.mkdir(exist_ok=True)

    print("=" * 78)
    print("IDB LATIN MACRO WATCH ANNUAL -> FISCAL SURVEY (SURVEY-FIRST)")
    print("=" * 78)
    print("1) Reading Latin Macro Watch package metadata for fallback/audit only ...")
    pkg = package_show(PACKAGE_ID)

    print("\n2) Downloading FULL Annual Series history from fixed IDB resource ...")
    resource, raw, acquisition_mode = fetch_annual_with_fallback(pkg)
    print(f"   Acquisition mode: {acquisition_mode}")
    print(f"   Resource: {resource_name(resource)}")
    print(f"   Resource id: {resource.get('id')}")
    print(f"   raw rows={len(raw):,}, columns={len(raw.columns):,}")

    with open(meta_dir / "package_metadata.json", "w", encoding="utf-8") as f:
        json.dump(pkg, f, ensure_ascii=False, indent=2, default=str)

    annual = normalize_lmw(raw)
    print(f"   annual normalized rows={len(annual):,}")
    if not annual.empty:
        print(f"   years={int(annual['Year'].min())}-{int(annual['Year'].max())}; countries={annual['Country'].nunique():,}; indicators={annual['Indicator'].nunique():,}")
    annual.to_csv(data_dir / "00_idb_annual_full_source.csv", index=False, encoding="utf-8-sig")

    print("\n3) Building IDB Annual series inventory ...")
    inv = make_series_inventory(annual)
    inv.to_csv(meta_dir / "07_idb_annual_series_inventory.csv", index=False, encoding="utf-8-sig")
    print(f"   distinct annual series={len(inv):,}")

    print("\n4) Mapping IDB Annual series to the EXISTING Fiscal Survey 64-variable registry ...")
    cand = build_mapping_candidates(inv)
    selected = select_best_mapping(cand)
    cand.to_csv(meta_dir / "06_mapping_candidates.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(meta_dir / "selected_mapping_internal.csv", index=False, encoding="utf-8-sig")

    rules = pd.DataFrame(FISCAL_MAPPING_RULES).rename(columns={
        "priority":"Priority", "category":"Category", "code":"Fiscal Survey Code",
        "variable":"Fiscal Survey Variable", "decision":"Discovery Rule Decision",
        "confidence":"Discovery Rule Confidence", "note":"Discovery Rule Note",
    })
    master = rules[[
        "Priority", "Category", "Fiscal Survey Code", "Fiscal Survey Variable",
        "Discovery Rule Decision", "Discovery Rule Confidence", "Discovery Rule Note"
    ]].copy()
    survey_map = build_survey_first_mapping(master, selected)
    survey_map.to_csv(meta_dir / "01_fiscal_survey_64_to_idb_mapping.csv", index=False, encoding="utf-8-sig")

    print("\n   === Fiscal Survey mapping result ===")
    print(f"   PRODUCTION = {(survey_map['Final Mapping Status']=='PRODUCTION').sum():,}")
    print(f"   REVIEW     = {(survey_map['Final Mapping Status']=='REVIEW').sum():,}")
    print(f"   GAP        = {(survey_map['Final Mapping Status']=='GAP').sum():,}")
    show_cols = [
        "Category", "Fiscal Survey Code", "Fiscal Survey Variable",
        "Final Mapping Status", "Mapped IDB Indicator", "IDB Unit", "Government Scope"
    ]
    print(survey_map[[c for c in show_cols if c in survey_map.columns]].to_string(index=False))

    print("\n5) Attaching Fiscal Survey identity to ACTUAL IDB observations ...")
    mapped = make_mapped_data(annual, selected)
    mapped.to_csv(data_dir / "01_idb_mapped_full_history_with_fiscal_survey_variables.csv", index=False, encoding="utf-8-sig")

    mapped_2007 = mapped[pd.to_numeric(mapped["year"], errors="coerce") >= SURVEY_START_YEAR].copy() if not mapped.empty else mapped
    mapped_2007.to_csv(data_dir / "02_idb_mapped_2007_latest_with_fiscal_survey_variables.csv", index=False, encoding="utf-8-sig")

    prod_2007 = mapped_2007[mapped_2007["Final Mapping Status"].eq("PRODUCTION")].copy() if not mapped_2007.empty else mapped_2007
    prod_2007.to_csv(data_dir / "03_idb_PRODUCTION_2007_latest_with_fiscal_survey_variables.csv", index=False, encoding="utf-8-sig")

    wide = build_wide(mapped, SURVEY_START_YEAR)
    wide.to_csv(data_dir / "04_fiscal_survey_country_year_wide_2007_latest.csv", index=False, encoding="utf-8-sig")

    print(f"   mapped full-history rows={len(mapped):,}")
    print(f"   mapped 2007-latest rows={len(mapped_2007):,}")
    print(f"   production 2007-latest rows={len(prod_2007):,}")

    print("\n6) Writing SURVEY-FIRST mapping workbook ...")
    wb = meta_dir / "IDB_Annual_to_Fiscal_Survey_SURVEY_FIRST.xlsx"
    export_mapping_workbook(wb, pkg, resource, inv, cand, selected, mapped)

    print("\nDONE")
    print(f"Output root: {OUTPUT_ROOT.resolve()}")
    print(f"Survey-first mapping CSV: {(meta_dir / '01_fiscal_survey_64_to_idb_mapping.csv').resolve()}")
    print(f"Mapped IDB data: {(data_dir / '02_idb_mapped_2007_latest_with_fiscal_survey_variables.csv').resolve()}")
    print(f"Mapping workbook: {wb.resolve()}")
    print("\nImportant: all Annual history is refreshed each run. The 2007 filter is applied only after mapping.")


if __name__ == "__main__":
    main()
