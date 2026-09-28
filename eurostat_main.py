from __future__ import annotations

import os

import csv
import gzip
import io
import json
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import pandas as pd

from common import get, standardize, source_settings, filter_years

SOURCE = "EUROSTAT"

INVENTORY_URL = "https://ec.europa.eu/eurostat/api/dissemination/files/inventory"
SDMX_DATAFLOW_BASE = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/dataflow/ESTAT"

CHUNK_SIZE = 200_000

# Official core annual GFS datasets.
CORE_DATASETS = [
    "gov_10a_main",
    "gov_10a_taxag",
    "gov_10a_exp",
]

DATASET_ALIASES = {
    # Human-facing/documentation spelling -> actual Eurostat bulk inventory code
    "gov_10a_tax_ag": "gov_10a_taxag",
    "gov_10a_taxag": "gov_10a_taxag",
}

FISCAL_MAPPING_RULES = [{'priority': 1,
  'category': 'Revenue',
  'code': 'GGREVTOTLCN',
  'variable': 'Total revenue',
  'include_any': ['government revenue', 'total revenue', 'total revenue'],
  'include_all': [],
  'exclude_any': ['tax', 'grant'],
  'decision': 'EXACT_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Exact known KIDB candidate; preserve institutional-scope/unit checks.'},
 {'priority': 2,
  'category': 'Revenue',
  'code': 'GGREVTAXTCN',
  'variable': 'Tax revenue (including social contributions)',
  'include_any': ['tax revenue', 'government taxes', 'tax revenue', 'taxes and social contributions'],
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
  'include_any': ['income tax', 'taxes on income', 'taxes on income', 'current taxes on income'],
  'include_all': [],
  'exclude_any': ['personal', 'corporate', 'company'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Aggregate income-tax candidate.'},
 {'priority': 5,
  'category': 'Revenue',
  'code': 'GGREVDRPICN',
  'variable': 'Personal income tax',
  'include_any': ['personal income tax', 'individual income tax', 'personal tax', 'personal income tax'],
  'include_all': [],
  'exclude_any': ['corporate', 'company'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until definition/scope confirmed.'},
 {'priority': 6,
  'category': 'Revenue',
  'code': 'GGREVDRCICN',
  'variable': 'Corporate income tax',
  'include_any': ['corporate income tax', 'company income tax', 'corporate tax', 'corporate income tax'],
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
  'include_any': ['property tax', 'taxes on property', 'taxes on property'],
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
  'include_any': ['taxes on goods and services', 'goods and services tax', 'tax on goods and services', 'taxes on goods and services', 'taxes on production and imports'],
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
  'include_any': ['value added tax', 'value-added tax', 'vat', 'value added tax', 'vat'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until source definition confirmed.'},
 {'priority': 14,
  'category': 'Revenue',
  'code': 'GGREVEXSECN',
  'variable': 'Excise tax',
  'include_any': ['excise tax', 'excise taxes', 'excise duty', 'excise duties', 'excise duties', 'excise taxes'],
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
  'include_any': ['international trade tax', 'taxes on international trade', 'trade taxes', 'trade and transactions tax'],
  'include_all': [],
  'exclude_any': ['customs', 'import', 'export'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Aggregate trade-tax candidate.'},
 {'priority': 22,
  'category': 'Revenue',
  'code': 'GGREVCUSTCN',
  'variable': 'Customs & other import duties',
  'include_any': ['customs duty', 'customs duties', 'import duty', 'import duties', 'customs and import', 'customs duties', 'import duties'],
  'include_all': [],
  'exclude_any': ['export'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 23,
  'category': 'Revenue',
  'code': 'GGREVEXPTCN',
  'variable': 'Taxes on exports',
  'include_any': ['export tax', 'export taxes', 'export duty', 'export duties', 'taxes on exports'],
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
  'include_any': ['social contribution', 'social contributions', 'social security contribution', 'net social contributions', 'social contributions'],
  'include_all': [],
  'exclude_any': ['benefit'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 27,
  'category': 'Revenue',
  'code': 'GGREVTOTHCN',
  'variable': 'Other taxes',
  'include_any': ['other taxes', 'miscellaneous taxes', 'other current taxes'],
  'include_all': [],
  'exclude_any': ['direct', 'income', 'property', 'goods', 'excise', 'trade', 'resource'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'LOW',
  'note': 'Broad category; review required.'},
 {'priority': 28,
  'category': 'Revenue',
  'code': 'GGREVCOMMCN',
  'variable': 'Non-tax resource (commodity) revenues',
  'include_any': ['non-tax resource revenue', 'nontax resource revenue', 'commodity revenue', 'resource revenue'],
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
  'include_any': ['other revenue', 'miscellaneous revenue', 'other current transfers, revenue', 'other revenue'],
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
  'include_any': ['government expenditure', 'total expenditure', 'total expenditure'],
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
  'include_any': ['compensation of employees', 'employee compensation', 'wages and salaries', 'wages', 'salary', 'compensation of employees'],
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
                  'government consumption goods and services',
                  'intermediate consumption',
                  'use of goods and services'],
  'include_all': [],
  'exclude_any': ['tax'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Economic expenditure category.'},
 {'priority': 36,
  'category': 'Expenditure',
  'code': 'GGEXPINTPCN',
  'variable': 'Interest expense',
  'include_any': ['interest expense', 'interest expenditure', 'interest payment', 'interest payments', 'interest, expenditure', 'interest expenditure'],
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
  'include_any': ['subsidies to business', 'business subsidies', 'subsidies', 'subsidy', 'subsidies, expenditure', 'subsidies'],
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
  'include_any': ['capital expenditure', 'capital spending', 'development expenditure', 'capital expenditure', 'gross capital formation'],
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
                  'gross fixed capital formation',
                  'gross fixed capital formation',
                  'government investment'],
  'include_all': [],
  'exclude_any': ['private'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Verify whether definition matches Fiscal Survey capital investment.'},
 {'priority': 48,
  'category': 'Expenditure',
  'code': 'GGEXPKCFKCN',
  'variable': 'Consumption of fixed capital',
  'include_any': ['consumption of fixed capital', 'depreciation', 'consumption of fixed capital'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 49,
  'category': 'Expenditure',
  'code': 'GGEXPKTRNCN',
  'variable': 'Other capital expenditure',
  'include_any': ['other capital expenditure', 'capital transfers', 'other capital spending', 'capital transfers, expenditure'],
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
  'include_any': ['net lending net borrowing', 'net lending/net borrowing', 'overall fiscal balance', 'fiscal balance', 'net lending', 'net borrowing'],
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
                  'general government gross debt',
                  'government gross debt'],
  'include_all': [],
  'exclude_any': ['external debt', 'domestic debt', 'private'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Debt stock; verify general-government/public-sector scope.'},
 {'priority': 63,
  'category': 'Debt',
  'code': 'GGDBTEXTLCN',
  'variable': 'External gross debt',
  'include_any': ['external gross debt', 'government external debt', 'public external debt', 'external public debt', 'external government debt'],
  'include_all': [],
  'exclude_any': ['private', 'domestic'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Debt stock; verify government scope.'},
 {'priority': 64,
  'category': 'Debt',
  'code': 'GGDBTDOMTCN',
  'variable': 'Domestic gross debt',
  'include_any': ['domestic gross debt', 'government domestic debt', 'public domestic debt', 'domestic public debt'],
  'include_all': [],
  'exclude_any': ['external', 'private'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Debt stock; verify government scope.'}]

# ESA / Eurostat transaction codes that are particularly reliable.
# These are treated as stronger than text similarity when present in
# NA_ITEM / transaction-like dimensions.
EXACT_CODE_HINTS = {
    "TR": "GGREVTOTLCN",
    "TE": "GGEXPTOTLCN",
    "B9": "GGBALOVRACN",
    "B.9": "GGBALOVRACN",
    "D41R": "GGREVINTICN",
    "D.41R": "GGREVINTICN",
    "D41P": "GGEXPINTPCN",
    "D.41P": "GGEXPINTPCN",
    "D51R": "GGREVDRINCN",
    "D.51R": "GGREVDRINCN",
    "D59R": "GGREVTOTHCN",
    "D.59R": "GGREVTOTHCN",
    "D61R": "GGREVSSOCCN",
    "D.61R": "GGREVSSOCCN",
    "D3P": "GGEXPSUBSCN",
    "D.3P": "GGEXPSUBSCN",
    "D62P": "GGEXPSOBCN",
    "D.62P": "GGEXPSOBCN",
    "D9P": "GGEXPKTRNCN",
    "D.9P": "GGEXPKTRNCN",
    "P51G": "GGEXPKINVCN",
    "P.51G": "GGEXPKINVCN",
    "D1P": "GGEXPWAGECN",
    "D.1P": "GGEXPWAGECN",
}
EXACT_CODE_HINTS_NORM = {
    norm_code: fs_code
    for raw_code, fs_code in EXACT_CODE_HINTS.items()
    for norm_code in [str(raw_code).upper().replace("_","").replace(" ","").replace(".","")]
}


HARD_FALSE_POSITIVES = [
    "household final consumption",
    "private consumption",
    "deposit interest rate",
    "lending interest rate",
    "real interest rate",
    "population covered",
]

AUTO_DATASET_TERMS = [
    "government revenue",
    "government expenditure",
    "government finance",
    "government debt",
    "tax revenue",
    "taxes and social contributions",
    "government deficit",
    "general government",
    "edp",
    "public finance",
]

def norm(x):
    x = str(x or "").upper()
    x = x.replace("_", "").replace(" ", "")
    return x

def text_norm(x):
    return re.sub(r"\s+", " ", str(x or "").lower()).strip()

def first(df, *names):
    return next((x for x in names if x in df.columns), None)

def run_stamp_full():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def _append_query(url: str, **kwargs) -> str:
    parts = list(urlparse(url))
    query = dict(parse_qsl(parts[4], keep_blank_values=True))
    for k,v in kwargs.items():
        if v is not None:
            query[k] = str(v).lower() if isinstance(v,bool) else str(v)
    parts[4] = urlencode(query)
    return urlunparse(parts)

def _decode_inventory(content: bytes) -> str:
    for enc in ("utf-8-sig","utf-8","latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            pass
    return content.decode("utf-8", errors="replace")

def load_inventory() -> pd.DataFrame:
    r = get(
        INVENTORY_URL,
        params={"type":"data","lang":"en"},
        headers={"Accept":"*/*"},
    )
    text = _decode_inventory(r.content)

    try:
        df = pd.read_csv(io.StringIO(text), sep="\t", dtype=str, keep_default_na=False)
        if len(df.columns) <= 1:
            raise ValueError("single-column parse")
    except Exception:
        dialect = csv.Sniffer().sniff(text[:10000], delimiters="\t,;")
        df = pd.read_csv(
            io.StringIO(text),
            sep=dialect.delimiter,
            dtype=str,
            keep_default_na=False,
        )

    if df.empty:
        raise RuntimeError("Eurostat inventory downloaded but contained zero rows.")
    return df

def find_col(df: pd.DataFrame, exact_candidates=()):
    normalized = {str(c).strip().lower(): c for c in df.columns}
    for x in exact_candidates:
        if x.lower() in normalized:
            return normalized[x.lower()]
    return None

def inventory_entry(inventory: pd.DataFrame, dataset: str) -> dict:
    code_col = find_col(inventory, exact_candidates=("Code","code"))
    if not code_col:
        raise RuntimeError(
            f"Eurostat inventory has no Code column. Columns: {list(inventory.columns)}"
        )

    match = inventory[
        inventory[code_col].astype(str).str.strip().str.lower() == dataset.lower()
    ]
    if match.empty:
        raise RuntimeError(f"Dataset {dataset} not found in Eurostat inventory.")

    row = match.iloc[0].to_dict()

    csv_col = next(
        (
            c for c in inventory.columns
            if "data download url" in str(c).lower()
            and "csv" in str(c).lower()
        ),
        None,
    )
    if csv_col is None:
        csv_col = next(
            (
                c for c in inventory.columns
                if "url" in str(c).lower() and "csv" in str(c).lower()
            ),
            None,
        )

    if csv_col is None or not str(row.get(csv_col,"")).strip():
        raise RuntimeError(f"No CSV bulk-download URL found for {dataset}.")

    return {"row":row,"csv_url":str(row[csv_col]).strip()}

def discover_fiscal_datasets(inventory, configured, max_auto_datasets=20):
    code_col = find_col(inventory, exact_candidates=("Code","code"))
    if not code_col:
        requested = []
        seen = set()
        for x in list(configured) + CORE_DATASETS:
            x = DATASET_ALIASES.get(str(x).strip().lower(), str(x).strip().lower())
            if x not in seen:
                requested.append(x)
                seen.add(x)
        return requested, pd.DataFrame({"dataset": requested})

    inv = inventory.copy()
    inv["_code_original"] = inv[code_col].astype(str).str.strip()
    inv["_code"] = inv["_code_original"].str.lower()

    text_cols = [c for c in inv.columns if c != code_col]
    inv["_search"] = (
        inv[text_cols].astype(str).fillna("").agg(" | ".join, axis=1).str.lower()
    )

    code_mask = inv["_code"].str.startswith(
        ("gov_10a","gov_10q","gov_10dd","gov_edp","gov_cl")
    )
    term_mask = inv["_search"].map(
        lambda x: any(t in x for t in AUTO_DATASET_TERMS)
    )

    auto = inv[code_mask | term_mask].copy()

    # Only official government-family dataset codes.
    auto = auto[auto["_code"].str.startswith("gov_")].copy()

    # Exclude derived/view inventory entries such as GOV_10A_EXP$DV_578.
    auto = auto[~auto["_code"].str.contains(r"\$", regex=True, na=False)].copy()

    # Normalize aliases before ranking/deduplication.
    auto["_code"] = auto["_code"].map(
        lambda x: DATASET_ALIASES.get(str(x).strip().lower(), str(x).strip().lower())
    )

    auto["priority"] = 5
    auto.loc[auto["_code"].isin(CORE_DATASETS), "priority"] = 0
    auto.loc[auto["_code"].str.startswith("gov_10a"), "priority"] = 1
    auto.loc[auto["_code"].str.startswith("gov_10q"), "priority"] = 2
    auto.loc[auto["_code"].str.startswith("gov_10dd"), "priority"] = 3

    auto = (
        auto.sort_values(["priority","_code"])
        .drop_duplicates("_code", keep="first")
        .head(max_auto_datasets)
    )

    requested = []
    seen = set()

    for x in list(configured) + CORE_DATASETS + auto["_code"].tolist():
        x = str(x).strip().lower()
        x = DATASET_ALIASES.get(x, x)

        # Never queue Eurostat derived-view codes.
        if "$" in x:
            continue

        if x not in seen:
            requested.append(x)
            seen.add(x)

    audit = auto.drop(columns=["priority"], errors="ignore").copy()
    return requested, audit

def save_structure(dataset: str, structure_dir: Path):
    url = f"{SDMX_DATAFLOW_BASE}/{dataset}/latest?references=all"
    try:
        r = get(url, headers={"Accept":"application/xml, text/xml, */*"})
        path = structure_dir / f"{dataset}_dataflow_latest.xml"
        path.write_bytes(r.content)
        return "SUCCESS", str(path)
    except Exception as e:
        return f"FAILED: {e}", ""

def parse_unique_code_labels(xml_file):
    """
    Extract SDMX code -> label pairs from Eurostat structure XML.

    Eurostat structures can expose names under different namespaces and nested
    structures. We therefore:
      1. inspect Code / Item elements with an id;
      2. search descendant Name elements, not only direct children;
      3. prefer English names when xml:lang is available;
      4. keep a code only when the resulting label is unambiguous.

    The function is intentionally conservative because the same short code can
    occur in different codelists.
    """
    if not xml_file or not Path(xml_file).exists():
        return {}

    try:
        root = ET.parse(xml_file).getroot()
    except Exception:
        return {}

    XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
    found = defaultdict(list)

    for elem in root.iter():
        lname = elem.tag.split("}")[-1]

        if lname not in ("Code", "Item"):
            continue

        code = elem.attrib.get("id")
        if not code:
            continue

        names = []
        for child in elem.iter():
            if child is elem:
                continue
            if child.tag.split("}")[-1] != "Name":
                continue

            txt = (child.text or "").strip()
            if not txt:
                continue

            lang = (child.attrib.get(XML_LANG) or child.attrib.get("lang") or "").lower()
            names.append((lang, txt))

        if not names:
            continue

        english = [txt for lang, txt in names if lang.startswith("en")]
        chosen = english[0] if english else names[0][1]
        found[str(code)].append(chosen)

    out = {}
    for code, labels in found.items():
        uniq = []
        for label in labels:
            if label not in uniq:
                uniq.append(label)

        # Keep unique/unambiguous labels only.
        if len(uniq) == 1:
            out[code] = uniq[0]

    return out

def enrich_dimension_labels(raw, code_labels):
    if not code_labels:
        return raw

    excluded = {
        "OBS_VALUE","VALUE","values","TIME_PERIOD","time","geo","GEO",
        "OBS_STATUS","OBS_FLAG","CONF_STATUS","DECIMALS",
    }

    for c in list(raw.columns):
        if c in excluded or c.endswith("__LABEL"):
            continue
        lab = raw[c].astype(str).map(code_labels)
        if lab.notna().any():
            raw[f"{c}__LABEL"] = lab

    return raw

def preserve_dimension_key(raw: pd.DataFrame) -> pd.Series:
    excluded = {
        "OBS_VALUE","VALUE","values","TIME_PERIOD","time",
        "geo","GEO","OBS_STATUS","OBS_FLAG","CONF_STATUS","DECIMALS",
    }

    dims = [
        c for c in raw.columns
        if c not in excluded and not c.endswith("__LABEL")
    ]
    if not dims:
        return pd.Series("", index=raw.index, dtype="string")

    parts = []
    for c in dims:
        base = raw[c].fillna("").astype(str).map(lambda x: f"{c}={x}")
        labcol = f"{c}__LABEL"
        if labcol in raw.columns:
            base = base + raw[labcol].fillna("").astype(str).map(
                lambda x: f"[{x}]" if x else ""
            )
        parts.append(base)

    out = parts[0]
    for p in parts[1:]:
        out = out + "|" + p
    return out

def download_to_local_temp(csv_url: str, target: Path):
    url = _append_query(csv_url, compressed=True)
    r = get(url, headers={"Accept":"*/*"}, stream=True)

    total = int(r.headers.get("content-length") or 0)
    done = 0

    with target.open("wb") as f:
        for block in r.iter_content(chunk_size=1024*1024):
            if not block:
                continue
            f.write(block)
            done += len(block)
            if total and done % (50*1024*1024) < 1024*1024:
                print(
                    f"      downloaded "
                    f"{done/1024/1024:,.0f}/{total/1024/1024:,.0f} MB"
                )
    return url

def _open_payload(path: Path):
    with path.open("rb") as f:
        sig = f.read(2)
    return gzip.open if sig == b"\x1f\x8b" else open

def iter_csv_chunks(path: Path):
    opener = _open_payload(path)

    with opener(
        path, "rt", encoding="utf-8-sig",
        errors="replace", newline=""
    ) as f:
        sample = f.read(20000)

        if not sample.strip():
            raise RuntimeError("Eurostat bulk file is empty.")

        low = sample.lstrip().lower()
        if low.startswith("<?xml") or low.startswith("<html") or low.startswith("<!doctype"):
            raise RuntimeError(
                "Eurostat bulk link returned XML/HTML instead of CSV. "
                f"Payload prefix: {sample[:300]}"
            )

        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            sep = dialect.delimiter
        except Exception:
            first_line = sample.splitlines()[0]
            counts = {
                ",":first_line.count(","),
                ";":first_line.count(";"),
                "\t":first_line.count("\t"),
            }
            sep = max(counts, key=counts.get)

        print(f"      gzip={opener is gzip.open}, delimiter={repr(sep)}")
        f.seek(0)

        for chunk in pd.read_csv(
            f, sep=sep, dtype=str,
            chunksize=CHUNK_SIZE, low_memory=False
        ):
            yield chunk

def _append_csv(df, path, first):
    df.to_csv(
        path,
        mode="w" if first else "a",
        header=first,
        index=False,
        encoding="utf-8-sig" if first else "utf-8",
    )

def _finalize(local_file, final_file):
    final_file.parent.mkdir(parents=True, exist_ok=True)
    if final_file.exists():
        final_file.unlink()

    try:
        shutil.move(str(local_file), str(final_file))
    except Exception:
        shutil.copy2(local_file, final_file)
        local_file.unlink(missing_ok=True)

def update_series_coverage(acc, raw, dataset, ycol, vcol):
    if raw.empty:
        return

    unit_col = first(raw, "unit","UNIT","UNIT_MEASURE")
    sector_col = first(raw, "sector","SECTOR")

    years = pd.to_numeric(raw[ycol], errors="coerce")

    temp = pd.DataFrame({
        "dataset": dataset,
        "indicator_key": raw["INDICATOR_KEY"].astype(str),
        "unit": raw[unit_col].astype(str) if unit_col else "",
        "sector": raw[sector_col].astype(str) if sector_col else "",
        "year": years,
        "value": pd.to_numeric(raw[vcol], errors="coerce"),
    })

    grouped = temp[temp["value"].notna()].groupby(
        ["dataset","indicator_key","unit","sector"],
        dropna=False
    )

    for key,g in grouped:
        rec = acc.setdefault(key, {
            "rows":0,
            "min_year":None,
            "max_year":None,
        })
        rec["rows"] += len(g)

        if g["year"].notna().any():
            mn = int(g["year"].min())
            mx = int(g["year"].max())
            rec["min_year"] = mn if rec["min_year"] is None else min(rec["min_year"],mn)
            rec["max_year"] = mx if rec["max_year"] is None else max(rec["max_year"],mx)

def exact_code_match(indicator_key):
    # Inspect all raw dimension values in the indicator key.
    values = re.findall(r"=([^|\[]+)", str(indicator_key))
    for v in values:
        nv = str(v).upper().replace("_","").replace(" ","").replace(".","")
        if nv in EXACT_CODE_HINTS_NORM:
            return EXACT_CODE_HINTS_NORM[nv], v
    return None, None

def score_rule(rule, indicator_key, dataset):
    text = text_norm(indicator_key)
    ds = text_norm(dataset)

    if any(text_norm(x) in text for x in rule["exclude_any"] if x):
        return None
    if any(text_norm(x) in text for x in HARD_FALSE_POSITIVES):
        return None

    required = [text_norm(x) for x in rule["include_all"] if x]
    if required and not all(x in text for x in required):
        return None

    hits = [x for x in rule["include_any"] if text_norm(x) in text]
    if not hits:
        return None

    score = 50 + max(len(text_norm(x).split()) for x in hits)*20 + len(hits)*5

    if dataset in CORE_DATASETS:
        score += 12
    if dataset == "gov_10a_main":
        score += 8
    if dataset == "gov_10a_tax_ag" and rule["category"] == "Revenue":
        score += 15
    if dataset == "gov_10a_exp" and rule["category"] == "Expenditure":
        score += 8

    return score, " | ".join(hits)

def build_mapping(series_inventory):
    candidates = []

    for _,rec in series_inventory.iterrows():
        exact_code, matched_code = exact_code_match(rec["indicator_key"])

        if exact_code:
            rule = next(
                (r for r in FISCAL_MAPPING_RULES if r["code"] == exact_code),
                None
            )
            if rule:
                candidates.append({
                    "priority":rule["priority"],
                    "category":rule["category"],
                    "fiscal_survey_code":rule["code"],
                    "fiscal_survey_variable":rule["variable"],
                    "mapping_status":"EXACT_ESA_CODE",
                    "mapping_confidence":"HIGH",
                    "rule_note":"Matched by Eurostat/ESA transaction code.",
                    "dataset":rec["dataset"],
                    "eurostat_indicator_key":rec["indicator_key"],
                    "unit":rec["unit"],
                    "sector":rec["sector"],
                    "rows":rec["rows"],
                    "min_year":rec["min_year"],
                    "max_year":rec["max_year"],
                    "matched_phrases":f"ESA code {matched_code}",
                    "mapping_score":200,
                })

        for rule in FISCAL_MAPPING_RULES:
            z = score_rule(rule, rec["indicator_key"], rec["dataset"])
            if z is None:
                continue
            score,hits = z
            candidates.append({
                "priority":rule["priority"],
                "category":rule["category"],
                "fiscal_survey_code":rule["code"],
                "fiscal_survey_variable":rule["variable"],
                "mapping_status":rule["decision"],
                "mapping_confidence":rule["confidence"],
                "rule_note":rule["note"],
                "dataset":rec["dataset"],
                "eurostat_indicator_key":rec["indicator_key"],
                "unit":rec["unit"],
                "sector":rec["sector"],
                "rows":rec["rows"],
                "min_year":rec["min_year"],
                "max_year":rec["max_year"],
                "matched_phrases":hits,
                "mapping_score":score,
            })

    cand = pd.DataFrame(candidates)
    if cand.empty:
        return cand,cand

    # Prefer exact ESA-code matches, then general-government sector if present,
    # then score and coverage.
    cand["_gg_bonus"] = cand["sector"].astype(str).str.upper().eq("S13").astype(int) * 20

    best = (
        cand.sort_values(
            [
                "fiscal_survey_code",
                "mapping_score",
                "_gg_bonus",
                "rows",
                "priority",
            ],
            ascending=[True,False,False,False,True]
        )
        .drop_duplicates("fiscal_survey_code",keep="first")
        .drop(columns=["_gg_bonus"])
        .reset_index(drop=True)
    )
    cand = cand.drop(columns=["_gg_bonus"])
    return cand,best


EXCEL_MAX_DATA_ROWS = 1_000_000

def write_df_excel_split(writer, df, base_sheet_name, max_rows=EXCEL_MAX_DATA_ROWS):
    """Write a DataFrame to one or more Excel sheets under Excel row limits."""
    if df is None:
        df = pd.DataFrame()

    used = []

    if len(df) == 0:
        sheet = base_sheet_name[:31]
        df.to_excel(writer, sheet_name=sheet, index=False)
        return [sheet]

    n_parts = (len(df) + max_rows - 1) // max_rows

    for part in range(n_parts):
        start = part * max_rows
        end = min((part + 1) * max_rows, len(df))

        if part == 0:
            sheet = base_sheet_name[:31]
        else:
            suffix = f" {part + 1}"
            sheet = (base_sheet_name[:31-len(suffix)] + suffix)[:31]

        df.iloc[start:end].to_excel(
            writer,
            sheet_name=sheet,
            index=False
        )
        used.append(sheet)

    return used

def export_runtime_excel(
    path, inventory_audit, dataset_summary,
    series_inventory, candidates, best
):
    rules_df = pd.DataFrame(FISCAL_MAPPING_RULES).rename(columns={
        "priority":"Priority",
        "category":"Category",
        "code":"Fiscal Survey Code",
        "variable":"Fiscal Survey Variable",
        "decision":"Auto Decision",
        "confidence":"Confidence",
        "note":"Rule Note",
        "include_any":"Include Any",
        "include_all":"Include All",
        "exclude_any":"Exclude Any",
    })

    full = rules_df[
        ["Priority","Category","Fiscal Survey Code","Fiscal Survey Variable",
         "Auto Decision","Confidence","Rule Note"]
    ].copy()

    if not best.empty:
        b = best.rename(columns={
            "fiscal_survey_code":"Fiscal Survey Code",
            "dataset":"Eurostat Dataset",
            "eurostat_indicator_key":"Eurostat Indicator Key",
            "mapping_status":"Runtime Status",
            "mapping_confidence":"Runtime Confidence",
            "mapping_score":"Runtime Score",
            "matched_phrases":"Matched Phrases",
            "unit":"Unit",
            "sector":"Sector",
            "rows":"Rows",
            "min_year":"Min Year",
            "max_year":"Max Year",
        })
        cols = [
            "Fiscal Survey Code","Eurostat Dataset","Eurostat Indicator Key",
            "Runtime Status","Runtime Confidence","Runtime Score",
            "Matched Phrases","Unit","Sector","Rows","Min Year","Max Year"
        ]
        full = full.merge(b[cols],on="Fiscal Survey Code",how="left")

    for c in [
        "Eurostat Dataset","Eurostat Indicator Key",
        "Runtime Status","Runtime Confidence","Runtime Score",
        "Matched Phrases","Unit","Sector","Rows","Min Year","Max Year"
    ]:
        if c not in full.columns:
            full[c] = ""

    full["Final Runtime Status"] = full["Runtime Status"].fillna("")
    full.loc[full["Final Runtime Status"].eq(""),"Final Runtime Status"] = "GAP"
    mapped = full[full["Final Runtime Status"]!="GAP"].copy()

    summary = pd.DataFrame([
        {"Metric":"Fiscal Survey variables","Value":len(FISCAL_MAPPING_RULES)},
        {"Metric":"Eurostat datasets queued","Value":len(inventory_audit)},
        {"Metric":"Datasets downloaded successfully","Value":int((dataset_summary["status"]=="SUCCESS").sum()) if not dataset_summary.empty else 0},
        {"Metric":"Downloaded unique dimension series","Value":len(series_inventory)},
        {"Metric":"Runtime mapping candidate links","Value":len(candidates)},
        {"Metric":"Mapped Fiscal Survey codes","Value":mapped["Fiscal Survey Code"].nunique()},
        {"Metric":"Remaining GAP codes","Value":int((full["Final Runtime Status"]=="GAP").sum())},
    ])

    with pd.ExcelWriter(path,engine="openpyxl") as w:
        write_df_excel_split(w, summary, "Summary")
        write_df_excel_split(w, full, "Eurostat Full Mapping")
        write_df_excel_split(w, mapped, "Eurostat Mapped Only")

        series_sheets = write_df_excel_split(
            w, series_inventory, "Series Inventory"
        )
        candidate_sheets = write_df_excel_split(
            w, candidates, "Runtime Candidates"
        )

        write_df_excel_split(
            w, dataset_summary, "Dataset Download Summary"
        )
        write_df_excel_split(
            w, inventory_audit, "Dataset Discovery"
        )
        write_df_excel_split(
            w, rules_df, "Embedded Mapping Rules"
        )

        from copy import copy
        for ws in w.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                f = copy(cell.font)
                f.bold = True
                cell.font = f
            for col_cells in ws.columns:
                letter = col_cells[0].column_letter
                max_len = max(
                    (len("" if c.value is None else str(c.value))
                     for c in col_cells[:200]),
                    default=10
                )
                ws.column_dimensions[letter].width = min(max(max_len+2,10),45)

    csv_dir = Path(path).parent
    series_inventory.to_csv(
        csv_dir / "eurostat_fiscal_series_inventory_FULL.csv",
        index=False,
        encoding="utf-8-sig"
    )
    candidates.to_csv(
        csv_dir / "eurostat_to_fiscal_mapping_candidates_FULL.csv",
        index=False,
        encoding="utf-8-sig"
    )

    print(
        f"  Excel split: Series Inventory -> {len(series_sheets)} sheet(s); "
        f"Runtime Candidates -> {len(candidate_sheets)} sheet(s)"
    )

    return {
        "mapped_codes":int(mapped["Fiscal Survey Code"].nunique()),
        "gaps":int((full["Final Runtime Status"]=="GAP").sum()),
        "candidate_links":int(len(candidates)),
    }


EUROSTAT_STATS_API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"

def _parse_locked_indicator_key(key):
    out={}
    for piece in str(key or "").split("|"):
        if "=" not in piece: continue
        k,v=piece.split("=",1); k=k.strip(); v=v.split("[",1)[0].strip()
        if k and v: out[k]=v
    return out

def _jsonstat_to_long(js):
    ids=list(js.get("id") or []); sizes=list(js.get("size") or [])
    if not ids or not sizes: return pd.DataFrame()
    dims=js.get("dimension") or {}
    cats=[]
    for d in ids:
        idx=((dims.get(d) or {}).get("category") or {}).get("index") or {}
        if isinstance(idx,dict):
            vals=[k for k,_ in sorted(idx.items(),key=lambda kv: kv[1])]
        else: vals=list(idx)
        cats.append(vals)
    import itertools
    value=js.get("value") or {}
    rows=[]
    for flat,combo in enumerate(itertools.product(*cats)):
        val=value.get(str(flat), value.get(flat)) if isinstance(value,dict) else (value[flat] if flat < len(value) else None)
        if val is None: continue
        r=dict(zip(ids,combo)); r["OBS_VALUE"]=val; rows.append(r)
    return pd.DataFrame(rows)

def _annual_locked_direct(cfg):
    from common import load_locked_plan
    plan=load_locked_plan(SOURCE); reg=pd.DataFrame(plan.get("rows") or []).fillna("")
    if reg.empty: raise RuntimeError("EUROSTAT annual LOCKED plan has no rows")
    ren={"Eurostat Dataset":"source_dataset","Eurostat Indicator Key":"indicator_original","Fiscal Survey Code":"wbfd_mnemonic","Fiscal Survey Variable":"wbfd_variable"}
    reg=reg.rename(columns={k:v for k,v in ren.items() if k in reg.columns})
    for c in ["source_dataset","indicator_original","wbfd_mnemonic","wbfd_variable"]:
        if c not in reg: reg[c]=""
    reg["source_dataset"]=reg["source_dataset"].astype(str).str.strip().str.lower(); reg["wbfd_mnemonic"]=reg["wbfd_mnemonic"].astype(str).str.strip().str.upper()
    reg=reg[(reg.source_dataset!='')&(reg.indicator_original.astype(str).str.strip()!='')&(reg.wbfd_mnemonic!='')].copy()
    if reg.empty: raise RuntimeError("EUROSTAT LOCKED plan has zero exact dataset+indicator signatures")
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S"); base=Path(f"eurostat_data_latest_{stamp}"); data_dir,meta_dir,log_dir=base/"data",base/"metadata",base/"logs"
    for d in (data_dir,meta_dir,log_dir): d.mkdir(parents=True,exist_ok=True)
    reg.to_csv(meta_dir/"eurostat_locked_registry_used.csv",index=False,encoding="utf-8-sig")
    parts=[]; audit=[]
    for i,rr in reg.reset_index(drop=True).iterrows():
        ds=rr["source_dataset"]; key=rr["indicator_original"]; params={"lang":"en","sinceTimePeriod":str(cfg.get("start_year",2007))}
        if cfg.get("end_year") is not None: params["untilTimePeriod"]=str(cfg["end_year"])
        filt=_parse_locked_indicator_key(key)
        for k,v in filt.items():
            if k.lower() in {"geo","time","time_period"}: continue
            params[k]=v
        print(f"  EUROSTAT DIRECT LOCKED [{i+1}/{len(reg)}] {ds} -> {rr['wbfd_mnemonic']}",flush=True)
        try:
            js=get(f"{EUROSTAT_STATS_API}/{ds}",params=params,headers={"Accept":"application/json"}).json()
            raw=_jsonstat_to_long(js)
            if raw.empty: raise RuntimeError("Eurostat JSON-stat query returned zero observations")
            ycol="time" if "time" in raw.columns else ("TIME_PERIOD" if "TIME_PERIOD" in raw.columns else None)
            ccol="geo" if "geo" in raw.columns else ("GEO" if "GEO" in raw.columns else None)
            if not ycol or not ccol: raise RuntimeError(f"Eurostat response missing geo/time: {list(raw.columns)}")
            raw=filter_years(raw,ycol,SOURCE)
            raw["INDICATOR_KEY"]=key
            unitcol="unit" if "unit" in raw.columns else None
            std=standardize(raw,"EUROSTAT",ccol,ycol,"OBS_VALUE","INDICATOR_KEY",unitcol,None)
            std["SOURCE_DATASET"]=ds; std["wbfd_mnemonic"]=rr["wbfd_mnemonic"]; std["wbfd_variable"]=rr["wbfd_variable"]
            std["mapping_status"]="LOCKED_DIRECT"; std["mapping_confidence"]="HIGH"; std["mapping_method"]="EUROSTAT_LOCKED_SIGNATURE_DIRECT_API"
            parts.append(std); audit.append({"dataset":ds,"wbfd_mnemonic":rr["wbfd_mnemonic"],"rows":len(std),"status":"SUCCESS"})
        except Exception as e:
            audit.append({"dataset":ds,"wbfd_mnemonic":rr["wbfd_mnemonic"],"rows":0,"status":"FAILED","message":str(e)}); raise
    prod=pd.concat(parts,ignore_index=True,sort=False) if parts else pd.DataFrame()
    prod.to_csv(data_dir/"eurostat_DIRECT_LOCKED_PRODUCTION.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(audit).to_csv(log_dir/"download_summary_latest.csv",index=False,encoding="utf-8-sig")
    print(f"  EUROSTAT DIRECT LOCKED DONE: rows={len(prod):,}; codes={prod['wbfd_mnemonic'].nunique() if not prod.empty else 0}")
    return pd.DataFrame(audit)

def main():
    cfg = source_settings(SOURCE)
    if os.environ.get("WBFD_ANNUAL_LOCKED_MODE", "").strip() == "1" and cfg.get("locked_only"):
        return _annual_locked_direct(cfg)

    configured = [
        DATASET_ALIASES.get(str(x).strip().lower(), str(x).strip().lower())
        for x in (cfg.get("datasets") or [])
        if str(x).strip()
    ]

    stamp = run_stamp_full()
    base = Path(f"eurostat_data_latest_{stamp}")
    data_dir = base/"data"
    structure_dir = base/"structure"
    log_dir = base/"logs"
    metadata_dir = base/"metadata"

    for p in (data_dir,structure_dir,log_dir,metadata_dir):
        p.mkdir(parents=True,exist_ok=True)

    inventory = load_inventory()
    inventory.to_csv(
        structure_dir/"eurostat_data_inventory.csv",
        index=False,encoding="utf-8-sig"
    )

    if cfg.get("locked_only"):
        datasets = list(dict.fromkeys(configured))
        discovery = pd.DataFrame({"dataset": datasets, "selection_reason": "LOCKED_REGISTRY_REQUIRED"})
        print("  Eurostat selective annual mode: auto-discovery disabled")
    else:
        datasets, discovery = discover_fiscal_datasets(
            inventory, configured, max_auto_datasets=int(cfg.get("max_auto_datasets",20))
        )

    discovery.to_csv(
        metadata_dir/"discovered_fiscal_datasets.csv",
        index=False,encoding="utf-8-sig"
    )

    print(f"  Eurostat: {len(datasets)} datasets queued")
    print("  Eurostat datasets: " + ", ".join(datasets))

    summary = []
    coverage = {}

    with tempfile.TemporaryDirectory(prefix="wbfd_eurostat_") as local_tmp:
        local_tmp = Path(local_tmp)
        print(f"  Eurostat local temp: {local_tmp}")

        for dataset in datasets:
            print(f"  EUROSTAT {dataset}")

            download_file = local_tmp/f"{dataset}.download"
            local_raw = local_tmp/f"{dataset}_raw.csv"
            local_std = local_tmp/f"{dataset}_standardized.csv"
            final_raw = data_dir/f"{dataset}_latest_raw.csv"
            final_std = data_dir/f"{dataset}_latest_standardized.csv"

            try:
                entry = inventory_entry(inventory,dataset)

                # Fetch structure first so dimension labels can enrich the bulk data.
                structure_status,structure_file = save_structure(
                    dataset,structure_dir
                )
                code_labels = parse_unique_code_labels(structure_file)

                resolved_url = download_to_local_temp(
                    entry["csv_url"],download_file
                )

                print(f"      parsing in chunks of {CHUNK_SIZE:,} rows")

                total_rows = 0
                min_year = None
                max_year = None
                first_raw = True
                first_std = True
                download_stamp = datetime.now().isoformat(timespec="seconds")

                for chunk_no,raw in enumerate(iter_csv_chunks(download_file),1):
                    ycol = first(raw,"TIME_PERIOD","time","TIME","Time")
                    if not ycol:
                        raise RuntimeError(
                            "TIME_PERIOD column not found. "
                            f"Columns: {list(raw.columns)[:50]}"
                        )

                    raw = filter_years(raw,ycol,SOURCE)
                    if raw.empty:
                        continue

                    vcol = first(raw,"OBS_VALUE","value","VALUE","Value")
                    if not vcol:
                        raise RuntimeError(
                            "OBS_VALUE/value column not found. "
                            f"Columns: {list(raw.columns)[:50]}"
                        )

                    raw = enrich_dimension_labels(raw,code_labels)
                    raw["PERIOD_ORIGINAL"] = raw[ycol].astype(str)
                    raw["INDICATOR_KEY"] = preserve_dimension_key(raw)

                    update_series_coverage(
                        coverage,raw,dataset,ycol,vcol
                    )

                    std = standardize(
                        raw,
                        "EUROSTAT",
                        country_col=first(raw,"geo","GEO","REF_AREA"),
                        year_col=ycol,
                        value_col=vcol,
                        indicator_col="INDICATOR_KEY",
                        unit_col=first(raw,"unit","UNIT","UNIT_MEASURE"),
                        currency_col=first(raw,"currency","CURRENCY"),
                    )

                    for df in (raw,std):
                        df["SOURCE_DATASET"] = dataset
                        df["DOWNLOAD_TIMESTAMP"] = download_stamp
                        df["RETRIEVAL_METHOD"] = (
                            "EUROSTAT_INVENTORY_BULK_CSV_LOCALTEMP_CHUNKED_FULLDISCOVERY"
                        )

                    _append_csv(raw,local_raw,first_raw)
                    _append_csv(std,local_std,first_std)
                    first_raw = False
                    first_std = False

                    years = pd.to_numeric(std["year"],errors="coerce")
                    if years.notna().any():
                        cmin = int(years.min())
                        cmax = int(years.max())
                        min_year = cmin if min_year is None else min(min_year,cmin)
                        max_year = cmax if max_year is None else max(max_year,cmax)

                    total_rows += len(std)

                    if chunk_no == 1 or chunk_no % 10 == 0:
                        print(
                            f"      processed chunks={chunk_no:,}, "
                            f"kept rows={total_rows:,}"
                        )

                if total_rows == 0:
                    raise RuntimeError(
                        f"{dataset} has no observations after year filtering."
                    )

                _finalize(local_raw,final_raw)
                _finalize(local_std,final_std)

                (structure_dir/f"{dataset}_inventory_entry.json").write_text(
                    json.dumps(entry["row"],ensure_ascii=False,indent=2),
                    encoding="utf-8"
                )

                summary.append({
                    "dataset":dataset,
                    "rows":total_rows,
                    "actual_min_year":min_year,
                    "actual_max_year":max_year,
                    "status":"SUCCESS",
                    "retrieval_method":"EUROSTAT_INVENTORY_BULK_CSV_LOCALTEMP_CHUNKED_FULLDISCOVERY",
                    "resolved_data_url":resolved_url,
                    "structure_status":structure_status,
                    "structure_file":structure_file,
                    "raw_file":str(final_raw),
                    "standardized_file":str(final_std),
                })

                print(
                    f"    SUCCESS rows={total_rows:,} "
                    f"years={min_year}-{max_year} "
                    f"labels={len(code_labels):,}"
                )
                if len(code_labels) == 0:
                    print(
                        "      NOTE: structure labels were not recovered; "
                        "raw Eurostat dimension codes are still preserved and "
                        "ESA exact-code mapping remains active."
                    )

            except Exception as e:
                print(f"    FAILED: {e}")
                summary.append({
                    "dataset":dataset,
                    "rows":0,
                    "status":"FAILED",
                    "message":str(e),
                })

    summary_df = pd.DataFrame(summary)
    summary_df.to_csv(
        log_dir/"download_summary_latest.csv",
        index=False,encoding="utf-8-sig"
    )

    # Convert accumulated coverage to runtime series inventory.
    series_rows = []
    for (dataset,key,unit,sector),rec in coverage.items():
        series_rows.append({
            "dataset":dataset,
            "indicator_key":key,
            "unit":unit,
            "sector":sector,
            **rec,
        })

    series_inventory = pd.DataFrame(series_rows)
    series_inventory.to_csv(
        metadata_dir/"eurostat_fiscal_series_inventory.csv",
        index=False,encoding="utf-8-sig"
    )

    if os.environ.get("WBFD_SKIP_RUNTIME_MAPPING") == "1":
        print("  Eurostat annual mode: FULL historical data downloaded; runtime mapping discovery/export SKIPPED", flush=True)
    else:
        candidates,best = build_mapping(series_inventory)
        candidates.to_csv(metadata_dir/"eurostat_to_fiscal_mapping_candidates.csv", index=False,encoding="utf-8-sig")
        best.to_csv(metadata_dir/"eurostat_to_fiscal_mapping_best.csv", index=False,encoding="utf-8-sig")
        runtime = metadata_dir/"EUROSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx"
        dataset_audit = pd.DataFrame({"dataset":datasets})
        print(f"  Eurostat runtime export: series_inventory={len(series_inventory):,}, mapping_candidates={len(candidates):,}")
        stats = export_runtime_excel(runtime,dataset_audit,summary_df,series_inventory,candidates,best)
        print(f"\n  Eurostat mapping: mapped Fiscal Survey codes={stats['mapped_codes']}, gaps={stats['gaps']}, candidate_links={stats['candidate_links']}")
        print(f"  Eurostat runtime mapping workbook: {runtime}")
    print(f"\nEurostat output root: {base.resolve()}")

    return summary_df

if __name__ == "__main__":
    main()
