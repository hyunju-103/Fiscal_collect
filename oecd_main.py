from __future__ import annotations

import os

import re
import json
import time
from io import StringIO
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET

import pandas as pd

from common import get, source_settings, request_settings, run_stamp

SOURCE = "OECD"
AGENCY = "OECD.GOV.GIP"
REST = "https://sdmx.oecd.org/public/rest"
DATAFLOW_CATALOG_URL = f"{REST}/dataflow/all"

# No observation-year or dataset-year setting lives in this file.
# Both come from common.py / OECD catalogue discovery.
DATASET_SPECS = {
    "01_gov_public_finance": {
        "regex": r"^DSD_GOV@DF_GOV_PF_(\d{4})$",
        "key": "A......",
    },
    "02_gov_cofog": {
        "regex": r"^DSD_GOV_COFOG@DF_GOV_COFOG_(\d{4})$",
        "key": "A...PT_B1GQ+PT_OTE_S13+PT_GPROC_S13+PT_OTE_S13_FNC+PT_P5L_S13....",
    },
    "03_gov_financial_instruments": {
        "regex": r"^DSD_GOV_FIN_INSTR@DF_GOV_FIN_INSTR_(\d{4})$",
        "key": "A........",
    },
    "04_gov_transactions": {
        "regex": r"^DSD_GOV_TRANSACTION@DF_GOV_TRANSACTION_(\d{4})$",
        "key": "A...PT_B1GQ+PT_TAX_REV+PT_OTE_S13+PT_OTR_S13+PT_PCOS_S13+PT_EXP_OUT_S13....",
    },
    "05_gov_level": {
        "regex": r"^DSD_GOV_LEVEL@DF_GOV_LEVEL_(\d{4})$",
        "key": "A...PT_OTE_S13+PT_OTR_S13+PT_GPROC_S13+PT_P5L_S13...",
    },
}

# ---------------------------------------------------------------------
# Fiscal Survey mapping rules
# ---------------------------------------------------------------------
# OECD mapping is generated at runtime from the series actually downloaded.
# Rules are intentionally conservative: exact/near-exact matches rank above
# broad text matches, and ambiguous matches remain reviewable.

FISCAL_MAPPING_RULES = [{'priority': 1,
  'category': 'Revenue',
  'code': 'GGREVTOTLCN',
  'variable': 'Total revenue',
  'include_any': ['government revenue',
                  'total revenue',
                  'ingresos totales',
                  'ingresos del gobierno',
                  'ingresos públicos',
                  'ingresos publicos'],
  'include_all': [],
  'exclude_any': ['tax', 'grant'],
  'decision': 'EXACT_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Exact known KIDB candidate; preserve institutional-scope/unit checks.'},
 {'priority': 2,
  'category': 'Revenue',
  'code': 'GGREVTAXTCN',
  'variable': 'Tax revenue (including social contributions)',
  'include_any': ['tax revenue',
                  'government taxes',
                  'ingresos tributarios',
                  'recaudación tributaria',
                  'recaudacion tributaria'],
  'include_all': ['government'],
  'exclude_any': ['tax on', 'income tax', 'property tax', 'vat', 'excise', 'customs'],
  'decision': 'CONDITIONAL',
  'confidence': 'MEDIUM',
  'note': 'Only final if ADB definition includes social contributions.'},
 {'priority': 3,
  'category': 'Revenue',
  'code': 'GGREVDRCTCN',
  'variable': 'Direct taxes',
  'include_any': ['direct tax', 'direct taxes', 'impuestos directos'],
  'include_all': [],
  'exclude_any': ['indirect'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only; verify source definition.'},
 {'priority': 4,
  'category': 'Revenue',
  'code': 'GGREVDRINCN',
  'variable': 'Income tax',
  'include_any': ['income tax', 'taxes on income', 'impuestos sobre la renta', 'impuesto sobre la renta'],
  'include_all': [],
  'exclude_any': ['personal', 'corporate', 'company'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Aggregate income-tax candidate.'},
 {'priority': 5,
  'category': 'Revenue',
  'code': 'GGREVDRPICN',
  'variable': 'Personal income tax',
  'include_any': ['personal income tax',
                  'individual income tax',
                  'personal tax',
                  'impuesto sobre la renta personal',
                  'impuesto a la renta personal',
                  'impuesto sobre la renta de personas físicas',
                  'impuesto sobre la renta de personas fisicas'],
  'include_all': [],
  'exclude_any': ['corporate', 'company'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until definition/scope confirmed.'},
 {'priority': 6,
  'category': 'Revenue',
  'code': 'GGREVDRCICN',
  'variable': 'Corporate income tax',
  'include_any': ['corporate income tax',
                  'company income tax',
                  'corporate tax',
                  'impuesto sobre la renta de sociedades',
                  'impuesto a la renta empresarial',
                  'impuesto sobre sociedades'],
  'include_all': [],
  'exclude_any': ['resource', 'commodity', 'personal'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until definition/scope confirmed.'},
 {'priority': 7,
  'category': 'Revenue',
  'code': 'GGREVDRCRCN',
  'variable': 'Corporate tax, resource (commodity) revenues',
  'include_any': ['resource corporate tax',
                  'commodity corporate tax',
                  'resource income tax',
                  'impuestos sobre la renta de recursos naturales',
                  'impuesto sobre hidrocarburos',
                  'impuesto minero'],
  'include_all': ['corporate'],
  'exclude_any': ['personal'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Resource-sector corporate tax only.'},
 {'priority': 8,
  'category': 'Revenue',
  'code': 'GGREVDROICN',
  'variable': 'Other (unallocable income taxes)',
  'include_any': ['other income tax', 'unallocable income tax', 'otros impuestos sobre la renta'],
  'include_all': [],
  'exclude_any': ['personal', 'corporate'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 9,
  'category': 'Revenue',
  'code': 'GGREVDRPRCN',
  'variable': 'Taxes on property',
  'include_any': ['property tax', 'taxes on property', 'impuestos sobre la propiedad', 'impuestos patrimoniales'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until definition confirmed.'},
 {'priority': 10,
  'category': 'Revenue',
  'code': 'GGREVDROTCN',
  'variable': 'Other direct taxes',
  'include_any': ['other direct tax', 'otros impuestos directos'],
  'include_all': [],
  'exclude_any': ['income', 'property'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 11,
  'category': 'Revenue',
  'code': 'GGREVIDGSCN',
  'variable': 'Taxes on goods and services',
  'include_any': ['taxes on goods and services',
                  'goods and services tax',
                  'tax on goods and services',
                  'impuestos sobre bienes y servicios'],
  'include_all': [],
  'exclude_any': ['vat', 'value added', 'excise'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Aggregate GST-type tax candidate.'},
 {'priority': 12,
  'category': 'Revenue',
  'code': 'GGREVGNFSCN',
  'variable': 'General taxes on goods & services (incl. VAT/Sales)',
  'include_any': ['general taxes on goods and services',
                  'general sales tax',
                  'sales tax',
                  'impuestos generales sobre bienes y servicios',
                  'impuestos generales a las ventas'],
  'include_all': [],
  'exclude_any': ['excise', 'specific'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Verify whether VAT/sales taxes are included.'},
 {'priority': 13,
  'category': 'Revenue',
  'code': 'GGREVVATTCN',
  'variable': 'VAT',
  'include_any': ['value added tax',
                  'value-added tax',
                  'vat',
                  'impuesto al valor agregado',
                  'impuesto sobre el valor añadido',
                  'iva'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only until source definition confirmed.'},
 {'priority': 14,
  'category': 'Revenue',
  'code': 'GGREVEXSECN',
  'variable': 'Excise tax',
  'include_any': ['excise tax',
                  'excise taxes',
                  'excise duty',
                  'excise duties',
                  'impuestos selectivos',
                  'impuestos específicos',
                  'impuestos especificos',
                  'impuestos especiales'],
  'include_all': [],
  'exclude_any': ['fuel', 'tobacco', 'alcohol', 'sugar'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Aggregate excise candidate.'},
 {'priority': 15,
  'category': 'Revenue',
  'code': 'GGREVEXFLCN',
  'variable': 'Excise tax on fuel',
  'include_any': ['fuel excise',
                  'excise on fuel',
                  'fuel tax',
                  'petroleum excise',
                  'impuesto a los combustibles',
                  'impuestos sobre combustibles'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 16,
  'category': 'Revenue',
  'code': 'GGREVEXTBCN',
  'variable': 'Excise tax on tobacco',
  'include_any': ['tobacco excise', 'excise on tobacco', 'tobacco tax', 'impuesto al tabaco', 'impuestos sobre tabaco'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 17,
  'category': 'Revenue',
  'code': 'GGREVEXALCN',
  'variable': 'Excise tax on alcohol',
  'include_any': ['alcohol excise',
                  'excise on alcohol',
                  'alcohol tax',
                  'impuesto al alcohol',
                  'impuestos sobre bebidas alcohólicas',
                  'impuestos sobre bebidas alcoholicas'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 18,
  'category': 'Revenue',
  'code': 'GGREVEXSGCN',
  'variable': 'Excise tax on sugar-sweetened beverages',
  'include_any': ['sugar sweetened beverage tax',
                  'sugar-sweetened beverage tax',
                  'ssb tax',
                  'sugar tax',
                  'impuesto a bebidas azucaradas',
                  'impuesto sobre bebidas azucaradas'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 19,
  'category': 'Revenue',
  'code': 'GGREVEXOTCN',
  'variable': 'Other excise taxes',
  'include_any': ['other excise tax',
                  'other excise duties',
                  'otros impuestos selectivos',
                  'otros impuestos específicos',
                  'otros impuestos especificos'],
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
  'include_any': ['customs duty',
                  'customs duties',
                  'import duty',
                  'import duties',
                  'customs and import',
                  'derechos de aduana',
                  'aranceles',
                  'derechos de importación',
                  'derechos de importacion',
                  'impuestos a las importaciones'],
  'include_all': [],
  'exclude_any': ['export'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 23,
  'category': 'Revenue',
  'code': 'GGREVEXPTCN',
  'variable': 'Taxes on exports',
  'include_any': ['export tax',
                  'export taxes',
                  'export duty',
                  'export duties',
                  'impuestos a las exportaciones',
                  'derechos de exportación',
                  'derechos de exportacion'],
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
  'include_any': ['social contribution',
                  'social contributions',
                  'social security contribution',
                  'contribuciones sociales',
                  'cotizaciones sociales',
                  'contribuciones a la seguridad social'],
  'include_all': [],
  'exclude_any': ['benefit'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 27,
  'category': 'Revenue',
  'code': 'GGREVTOTHCN',
  'variable': 'Other taxes',
  'include_any': ['other taxes', 'miscellaneous taxes', 'otros impuestos'],
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
  'include_any': ['grants revenue',
                  'grant revenue',
                  'government grants received',
                  'grants received',
                  'donaciones',
                  'transferencias recibidas'],
  'include_all': [],
  'exclude_any': ['capital grant expenditure'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Revenue-side grants.'},
 {'priority': 30,
  'category': 'Revenue',
  'code': 'GGREVOTHRCN',
  'variable': 'Other revenue',
  'include_any': ['other revenue', 'miscellaneous revenue', 'otros ingresos', 'otros ingresos no tributarios'],
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
                  'gasto público total',
                  'gasto publico total',
                  'gastos totales',
                  'gasto del gobierno',
                  'gasto público',
                  'gasto publico'],
  'include_all': [],
  'exclude_any': ['net lending'],
  'decision': 'EXACT_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Exact known KIDB candidate; preserve scope/unit checks.'},
 {'priority': 33,
  'category': 'Expenditure',
  'code': 'GGEXPCRNTCN',
  'variable': 'Current expenditure',
  'include_any': ['current expenditure', 'current expense', 'gasto corriente', 'gastos corrientes'],
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
                  'salary',
                  'remuneración de asalariados',
                  'remuneracion de asalariados',
                  'sueldos y salarios',
                  'gasto en personal'],
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
                  'uso de bienes y servicios',
                  'bienes y servicios'],
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
                  'pago de intereses',
                  'pagos de intereses',
                  'gasto por intereses',
                  'intereses de la deuda'],
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
  'include_any': ['current transfers', 'current transfer', 'transferencias corrientes'],
  'include_all': [],
  'exclude_any': ['capital'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Candidate only.'},
 {'priority': 40,
  'category': 'Expenditure',
  'code': 'GGEXPTPNSCN',
  'variable': 'Social security benefits',
  'include_any': ['social security benefit',
                  'social benefits',
                  'social protection',
                  'prestaciones de seguridad social'],
  'include_all': [],
  'exclude_any': ['assistance'],
  'decision': 'PARTIAL_REVIEW',
  'confidence': 'LOW',
  'note': 'Functional social protection is not automatically equivalent to social-security benefits.'},
 {'priority': 41,
  'category': 'Expenditure',
  'code': 'GGEXPTSOCCN',
  'variable': 'Social assistance',
  'include_any': ['social assistance', 'social assistance benefits', 'asistencia social'],
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
  'include_any': ['subsidies to business', 'business subsidies', 'subsidies', 'subsidy', 'subsidios', 'subvenciones'],
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
  'include_any': ['capital expenditure',
                  'capital spending',
                  'development expenditure',
                  'gasto de capital',
                  'gastos de capital',
                  'gasto en capital',
                  'gasto de inversión',
                  'gasto de inversion'],
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
                  'inversión pública',
                  'inversion publica'],
  'include_all': [],
  'exclude_any': ['private'],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'MEDIUM',
  'note': 'Verify whether definition matches Fiscal Survey capital investment.'},
 {'priority': 48,
  'category': 'Expenditure',
  'code': 'GGEXPKCFKCN',
  'variable': 'Consumption of fixed capital',
  'include_any': ['consumption of fixed capital', 'depreciation', 'consumo de capital fijo'],
  'include_all': [],
  'exclude_any': [],
  'decision': 'AUTO_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Candidate only.'},
 {'priority': 49,
  'category': 'Expenditure',
  'code': 'GGEXPKTRNCN',
  'variable': 'Other capital expenditure',
  'include_any': ['other capital expenditure',
                  'capital transfers',
                  'other capital spending',
                  'otros gastos de capital'],
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
                  'balance global',
                  'saldo global',
                  'balance fiscal',
                  'resultado fiscal'],
  'include_all': [],
  'exclude_any': ['cash'],
  'decision': 'EXACT_CANDIDATE_BASIS_CHECK',
  'confidence': 'HIGH',
  'note': 'Known closest concept; verify accounting basis and institutional coverage.'},
 {'priority': 52,
  'category': 'Balance',
  'code': 'GGBALOVRLCN',
  'variable': 'Overall fiscal balance (cash / below-the-line)',
  'include_any': ['cash balance', 'cash fiscal balance', 'overall balance cash', 'balance de caja', 'saldo de caja'],
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
  'include_any': ['net external financing',
                  'external financing net',
                  'financiamiento externo',
                  'financiación externa',
                  'financiacion externa'],
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
                  'deuda pública total',
                  'deuda publica total',
                  'deuda del gobierno',
                  'deuda pública',
                  'deuda publica'],
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
                  'deuda pública externa',
                  'deuda publica externa',
                  'deuda externa del gobierno'],
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


def _norm_text(x):
    s = "" if x is None or pd.isna(x) else str(x).lower()
    s = re.sub(r"[^a-z0-9%]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _find_col(df: pd.DataFrame, names):
    return next((c for c in names if c in df.columns), None)


def _series_dimension_columns(df: pd.DataFrame):
    """
    Keep the OECD dimensions that identify a series.
    Exclude observation/value/metadata columns.
    """
    ignore = {
        "OBS_VALUE", "TIME_PERIOD", "OBS_STATUS", "DECIMALS",
        "SOURCE_DATASET", "DATAFLOW_ID", "DATASET_YEAR",
        "SDMX_VERSION", "DOWNLOAD_TIMESTAMP",
        "REF_AREA", "REF_AREA_LABEL", "Reference area",
    }
    return [c for c in df.columns if c not in ignore]


def build_series_inventory(downloaded_frames: list[pd.DataFrame]) -> pd.DataFrame:
    rows = []

    for df in downloaded_frames:
        if df is None or df.empty:
            continue

        dataset = (
            str(df["SOURCE_DATASET"].iloc[0])
            if "SOURCE_DATASET" in df.columns and len(df)
            else ""
        )

        dims = _series_dimension_columns(df)
        label_cols = [
            c for c in df.columns
            if c.upper().endswith("_LABEL")
            or c.upper().endswith("_NAME")
            or c.upper() in {
                "TRANSACTION", "TRANSACTION_NAME",
                "MEASURE", "MEASURE_NAME",
                "UNIT_MEASURE", "UNIT_MEASURE_NAME",
                "SECTOR", "SECTOR_NAME",
                "ACCOUNTING_ENTRY", "ACCOUNTING_ENTRY_NAME",
                "INSTR_ASSET", "INSTR_ASSET_NAME",
                "FUNCTION", "FUNCTION_NAME",
            }
        ]

        group_cols = [c for c in dims if c in df.columns]
        if not group_cols:
            continue

        grouped = df.groupby(group_cols, dropna=False)

        for key, g in grouped:
            if not isinstance(key, tuple):
                key = (key,)

            rec = dict(zip(group_cols, key))
            rec["dataset"] = dataset

            # Composite key used later by mapped_collector.
            rec["indicator_key"] = "|".join(
                f"{col}={rec.get(col)}"
                for col in group_cols
                if rec.get(col) is not None and not pd.isna(rec.get(col))
            )

            # Search text combines labels and codes.
            parts = []
            for c in label_cols:
                if c in g.columns:
                    vals = g[c].dropna().astype(str).unique().tolist()[:5]
                    parts.extend(vals)

            for c in group_cols:
                v = rec.get(c)
                if v is not None and not pd.isna(v):
                    parts.append(str(v))

            rec["search_text"] = " | ".join(dict.fromkeys(parts))

            if "TIME_PERIOD" in g.columns:
                yy = pd.to_numeric(g["TIME_PERIOD"], errors="coerce")
                rec["min_year"] = yy.min()
                rec["max_year"] = yy.max()
            else:
                rec["min_year"] = pd.NA
                rec["max_year"] = pd.NA

            if "REF_AREA" in g.columns:
                rec["countries"] = g["REF_AREA"].dropna().nunique()
            else:
                rec["countries"] = pd.NA

            rec["rows"] = len(g)
            rows.append(rec)

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


def build_mapping_candidates(series_inventory: pd.DataFrame):
    candidate_rows = []

    if series_inventory is None or series_inventory.empty:
        return pd.DataFrame(), pd.DataFrame()

    for _, srow in series_inventory.iterrows():
        hay = _norm_text(srow.get("search_text", ""))

        for rule in FISCAL_MAPPING_RULES:
            include_any = [_norm_text(x) for x in rule["include_any"] if _norm_text(x)]
            include_all = [_norm_text(x) for x in rule["include_all"] if _norm_text(x)]
            exclude_any = [_norm_text(x) for x in rule["exclude_any"] if _norm_text(x)]

            any_hits = [x for x in include_any if x in hay]
            all_ok = all(x in hay for x in include_all)
            excluded = [x for x in exclude_any if x in hay]

            if include_any and not any_hits:
                continue
            if not all_ok:
                continue
            if excluded:
                continue

            # Transparent, simple score.
            score = (
                100
                + 10 * len(any_hits)
                + 5 * len(include_all)
                - rule["priority"] * 0.01
            )

            row = {
                "dataset": srow.get("dataset"),
                "indicator_key": srow.get("indicator_key"),
                "search_text": srow.get("search_text"),
                "fiscal_survey_code": rule["code"],
                "fiscal_survey_variable": rule["variable"],
                "mapping_status": rule["decision"],
                "mapping_confidence": rule["confidence"],
                "mapping_score": score,
                "matched_terms": " | ".join(any_hits + include_all),
                "rule_priority": rule["priority"],
                "rows": srow.get("rows"),
                "countries": srow.get("countries"),
                "min_year": srow.get("min_year"),
                "max_year": srow.get("max_year"),
            }

            # Preserve actual OECD dimensions in the mapping output.
            for c in series_inventory.columns:
                if c not in row:
                    row[c] = srow.get(c)

            candidate_rows.append(row)

    candidates = pd.DataFrame(candidate_rows)
    if candidates.empty:
        return candidates, candidates.copy()

    candidates = candidates.sort_values(
        ["fiscal_survey_code", "mapping_score"],
        ascending=[True, False],
    ).reset_index(drop=True)

    candidates["candidate_rank"] = (
        candidates.groupby("fiscal_survey_code")
        .cumcount()
        .add(1)
    )

    best = (
        candidates[candidates["candidate_rank"].eq(1)]
        .copy()
        .reset_index(drop=True)
    )

    return candidates, best


def export_runtime_mapping_workbook(
    output_file: Path,
    series_inventory: pd.DataFrame,
    candidates: pd.DataFrame,
    best: pd.DataFrame,
):
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        series_inventory.to_excel(writer, sheet_name="SeriesInventory", index=False)
        candidates.to_excel(writer, sheet_name="MappingCandidates", index=False)
        best.to_excel(writer, sheet_name="BestMapping", index=False)

    return {
        "series": len(series_inventory),
        "candidate_links": len(candidates),
        "mapped_codes": best["fiscal_survey_code"].nunique() if not best.empty else 0,
    }



def _local(tag: str) -> str:
    return tag.split("}")[-1]


def discover_catalog() -> list[dict]:
    """Download OECD's full SDMX dataflow catalogue and return GOV.GIP flows."""
    r = get(DATAFLOW_CATALOG_URL, headers={"Accept": "application/vnd.sdmx.structure+xml;version=2.0"})
    root = ET.fromstring(r.content)
    rows = []
    for el in root.iter():
        if _local(el.tag) != "Dataflow":
            continue
        agency = el.attrib.get("agencyID") or el.attrib.get("agencyId")
        flow_id = el.attrib.get("id")
        version = el.attrib.get("version") or ""
        if agency == AGENCY and flow_id:
            rows.append({"agency": agency, "flow_id": flow_id, "version": version})
    if not rows:
        raise RuntimeError("No OECD.GOV.GIP dataflows found in OECD dataflow catalogue.")
    return rows


def _version_key(version: str):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", version))
    except Exception:
        return (0,)


def discover_latest_flows() -> dict[str, dict]:
    """Find the newest usable _YYYY dataflow for each fiscal dataset family."""
    cfg = source_settings(SOURCE)
    if not cfg.get("latest_vintage", True):
        raise ValueError("OECD is designed for latest_vintage=True in common.py.")

    catalog = discover_catalog()
    resolved = {}
    for dataset_name, spec in DATASET_SPECS.items():
        candidates = []
        rx = re.compile(spec["regex"])
        for row in catalog:
            m = rx.match(row["flow_id"])
            if not m:
                continue
            candidates.append({**row, "dataset_year": int(m.group(1))})
        if not candidates:
            raise RuntimeError(f"No OECD dataflow matched {spec['regex']}")

        # Newest vintage first; newest SDMX version within a vintage first.
        candidates.sort(key=lambda x: (x["dataset_year"], _version_key(x["version"])), reverse=True)

        selected = None
        for cand in candidates:
            structure_url = (
                f"{REST}/dataflow/{AGENCY}/{cand['flow_id']}/{cand['version']}?references=all"
            )
            try:
                get(structure_url)
                selected = cand
                break
            except Exception as e:
                print(f"[{dataset_name}] candidate rejected: {cand['flow_id']} {cand['version']} ({e})")
        if selected is None:
            raise RuntimeError(f"No usable OECD candidate found for {dataset_name}")

        resolved[dataset_name] = {**spec, **selected}
    return resolved


def build_urls(flow: dict) -> tuple[str, str]:
    cfg = source_settings(SOURCE)
    params = [f"startPeriod={cfg['start_year']}"]
    if cfg.get("end_year") is not None:
        params.append(f"endPeriod={cfg['end_year']}")
    params += ["dimensionAtObservation=AllDimensions", "format=csvfilewithlabels"]
    query = "&".join(params)

    data_url = (
        f"{REST}/data/{AGENCY},{flow['flow_id']},{flow['version']}/"
        f"{flow['key']}?{query}"
    )
    structure_url = (
        f"{REST}/dataflow/{AGENCY}/{flow['flow_id']}/{flow['version']}?references=all"
    )
    return data_url, structure_url


def download_data(data_url: str) -> pd.DataFrame:
    r = get(data_url, headers={"Accept": "text/csv"})
    df = pd.read_csv(StringIO(r.text))
    if df.empty:
        raise RuntimeError("OECD data response parsed successfully but contained zero rows.")
    return df


def save_structure(structure_url: str, output_file: Path) -> Path:
    r = get(structure_url)
    output_file.write_bytes(r.content)
    return output_file


def extract_structure_labels(dataset_name: str, dataset_year: int, structure_file: Path) -> pd.DataFrame:
    root = ET.parse(structure_file).getroot()
    rows = []
    for element in root.iter():
        if _local(element.tag) != "Codelist":
            continue
        codelist_id = element.attrib.get("id")
        codelist_name = None
        for child in element:
            if _local(child.tag) == "Name" and child.text:
                codelist_name = child.text.strip()
                break
        for child in element:
            if _local(child.tag) != "Code":
                continue
            code = child.attrib.get("id")
            label = None
            for code_child in child:
                if _local(code_child.tag) == "Name" and code_child.text:
                    label = code_child.text.strip()
                    break
            rows.append({
                "dataset": dataset_name,
                "dataset_year": dataset_year,
                "codelist_id": codelist_id,
                "codelist_name": codelist_name,
                "code": code,
                "label": label,
            })
    return pd.DataFrame(rows)



def _parse_oecd_locked_key(x):
    out={}
    for piece in str(x or "").split("|"):
        if "=" not in piece: continue
        k,v=piece.split("=",1); k=k.strip().upper(); v=v.split("[",1)[0].strip()
        if k and v: out[k]=v
    return out

def _oecd_dimension_order(structure_file, flow_id=""):
    root=ET.parse(structure_file).getroot()
    target_dsd=str(flow_id or "").split("@",1)[0].strip()
    candidates=[]
    for ds in root.iter():
        if _local(ds.tag)!="DataStructure": continue
        dsid=str(ds.attrib.get("id") or "")
        dims=[]
        for el in ds.iter():
            if _local(el.tag) not in {"Dimension","TimeDimension"}: continue
            did=el.attrib.get("id")
            if not did or did=="TIME_PERIOD": continue
            try: pos=int(el.attrib.get("position","999"))
            except Exception: pos=999
            dims.append((pos,did))
        if dims: candidates.append((dsid,dims))
    if candidates:
        chosen=next((dims for dsid,dims in candidates if target_dsd and dsid==target_dsd),None)
        if chosen is None: chosen=max(candidates,key=lambda z:len(z[1]))[1]
    else:
        chosen=[]
        for el in root.iter():
            if _local(el.tag) not in {"Dimension","TimeDimension"}: continue
            did=el.attrib.get("id")
            if not did or did=="TIME_PERIOD": continue
            try: pos=int(el.attrib.get("position","999"))
            except Exception: pos=999
            chosen.append((pos,did))
    best={}
    for pos,d in chosen: best[d]=min(pos,best.get(d,999))
    return [d for d,_ in sorted(best.items(),key=lambda kv:kv[1])]

def _oecd_physical_family(value: str) -> str:
    """Normalize a physical OECD dataflow id to a vintage-free family id.

    Examples
    --------
    DF_GOV_PF_2025 -> DF_GOV_PF
    DSD_GOV@DF_GOV_PF_2026 -> DF_GOV_PF
    """
    s = str(value or "").strip()
    if "@" in s:
        s = s.split("@", 1)[1]
    return re.sub(r"_\d{4}$", "", s)


def _resolve_oecd_locked_family(value: str, flows: dict[str, dict]) -> str | None:
    """Resolve a LOCKED dataset reference to the logical DATASET_SPECS family.

    LOCKED registries intentionally preserve the physical source dataset that was
    reviewed (for example DF_GOV_PF_2025).  Annual refresh must not require that
    exact vintage to still be the newest one.  Instead, map it to the stable
    family and use discover_latest_flows() to select the newest usable _YYYY flow.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw in flows:
        return raw

    target = _oecd_physical_family(raw)
    hits = []
    for family, flow in flows.items():
        candidates = {
            _oecd_physical_family(family),
            _oecd_physical_family(flow.get("flow_id", "")),
            _oecd_physical_family(str(flow.get("flow_id", "")).split("@", 1)[-1]),
        }
        if target and target in candidates:
            hits.append(family)

    hits = sorted(set(hits))
    return hits[0] if len(hits) == 1 else None


def _annual_locked_direct(cfg):
    from common import load_locked_plan
    plan=load_locked_plan(SOURCE); reg=pd.DataFrame(plan.get("rows") or []).fillna("")
    if reg.empty: raise RuntimeError("OECD annual LOCKED plan has no rows")
    ren={"Primary OECD Dataset":"source_dataset","Primary Source Code":"indicator_original","Fiscal Survey Code":"wbfd_mnemonic","Fiscal Survey Variable":"wbfd_variable"}
    reg=reg.rename(columns={k:v for k,v in ren.items() if k in reg.columns})
    for c in ["source_dataset","indicator_original","validated_filter","wbfd_mnemonic","wbfd_variable"]:
        if c not in reg: reg[c]=""
    reg["source_dataset"]=reg["source_dataset"].astype(str).str.strip(); reg["wbfd_mnemonic"]=reg["wbfd_mnemonic"].astype(str).str.strip().str.upper()
    reg=reg[(reg.source_dataset!='')&(reg.wbfd_mnemonic!='')].copy()
    if reg.empty: raise RuntimeError("OECD LOCKED plan has zero source dataset mappings")

    # Resolve reviewed physical vintages (e.g. DF_GOV_PF_2025) to stable logical
    # families, then use the newest currently usable vintage discovered from OECD.
    all_flows=discover_latest_flows()
    reg["_dataset_family"] = reg["source_dataset"].map(lambda x: _resolve_oecd_locked_family(x, all_flows) or "")
    unresolved = sorted(set(reg.loc[reg["_dataset_family"].eq(""), "source_dataset"].astype(str)))
    if unresolved:
        available = {k: v.get("flow_id", "") for k, v in all_flows.items()}
        raise RuntimeError(
            "OECD LOCKED dataset references could not be mapped to a current family: "
            f"{unresolved}. Current resolved families={available}"
        )
    wanted=set(reg["_dataset_family"])
    flows={k:v for k,v in all_flows.items() if k in wanted}
    missing=wanted-set(flows)
    if missing: raise RuntimeError(f"OECD locked dataset families not resolved after vintage normalization: {sorted(missing)}")

    for old in sorted(set(reg["source_dataset"])):
        fam = _resolve_oecd_locked_family(old, all_flows)
        cur = flows[fam].get("flow_id", "") if fam in flows else ""
        print(f"  OECD LOCKED vintage resolve: {old} -> {fam} -> {cur}", flush=True)
    years=sorted({x["dataset_year"] for x in flows.values()}); vintage=str(years[0]) if len(years)==1 else "mixed_latest"
    base=Path(f"oecd_data_{vintage}_{run_stamp()}"); data_dir,structure_dir,log_dir,meta_dir=base/"data",base/"structure",base/"logs",base/"metadata"
    for d in (data_dir,structure_dir,log_dir,meta_dir): d.mkdir(parents=True,exist_ok=True)
    reg.to_csv(meta_dir/"oecd_locked_registry_used.csv",index=False,encoding="utf-8-sig")
    parts=[]; audit=[]
    for dsno,(dataset_name,flow) in enumerate(flows.items(),1):
        _,structure_url=build_urls(flow)
        structure_file=save_structure(structure_url,structure_dir/f"{dataset_name}_{flow['dataset_year']}_structure.xml")
        order=_oecd_dimension_order(structure_file,flow.get("flow_id",""))
        rrset=reg[reg["_dataset_family"].eq(dataset_name)].copy()
        for j,rr in rrset.reset_index(drop=True).iterrows():
            filt={}
            filt.update(_parse_oecd_locked_key(rr.get("indicator_original","")))
            # validated_filter is often semicolon-separated rather than pipe-separated
            for k,v in re.findall(r'([A-Za-z_]+)\s*=\s*([^;,|]+)',str(rr.get("validated_filter",""))): filt[k.strip().upper()]=v.strip()
            for fld in ["TRANSACTION","MEASURE","UNIT_MEASURE","EXPENDITURE","INSTR_ASSET","ACCOUNTING_ENTRY","SECTOR","FREQ"]:
                val=str(rr.get(fld,"" )).strip()
                if val: filt[fld]=val
            seg=[]; used=0
            for dim in order:
                if dim=="REF_AREA": seg.append(""); continue
                val=filt.get(dim,"")
                if dim=="FREQ" and not val: val="A"
                seg.append(val)
                if val: used+=1
            if used==0: raise RuntimeError(f"OECD {dataset_name}/{rr['wbfd_mnemonic']}: no exact dimension filter in LOCKED registry")
            key=".".join(seg)
            params=[f"startPeriod={cfg.get('start_year',2007)}"]
            if cfg.get("end_year") is not None: params.append(f"endPeriod={cfg['end_year']}")
            params += ["dimensionAtObservation=AllDimensions","format=csvfilewithlabels"]
            url=f"{REST}/data/{AGENCY},{flow['flow_id']},{flow['version']}/{key}?{'&'.join(params)}"
            print(f"  OECD DIRECT LOCKED [{dsno}/{len(flows)}.{j+1}] {dataset_name} -> {rr['wbfd_mnemonic']}",flush=True)
            try:
                df=download_data(url)
                if "REF_AREA" not in df or "TIME_PERIOD" not in df or "OBS_VALUE" not in df: raise RuntimeError("OECD exact response missing REF_AREA/TIME_PERIOD/OBS_VALUE")
                indkey=rr.get("indicator_original","") or "|".join(f"{k}={v}" for k,v in sorted(filt.items()))
                df["_LOCKED_KEY"]=indkey
                unitcol="UNIT_MEASURE" if "UNIT_MEASURE" in df.columns else None
                std=pd.DataFrame({
                    "source":"OECD","country_original":df["REF_AREA"],"country_code":df["REF_AREA"],
                    "year":pd.to_numeric(df["TIME_PERIOD"],errors="coerce").astype("Int64"),
                    "indicator_original":indkey,"unit_original":df[unitcol] if unitcol else "",
                    "currency_original":"","value_original":pd.to_numeric(df["OBS_VALUE"],errors="coerce"),"value_lcu_mn":pd.NA,
                    "wbfd_mnemonic":rr["wbfd_mnemonic"],"wbfd_variable":rr["wbfd_variable"],"conversion_note":""
                })
                std["SOURCE_DATASET"]=str(flow.get("flow_id", "")).split("@",1)[-1]; std["locked_source_dataset"]=rr.get("source_dataset", ""); std["dataset_family"]=dataset_name; std["mapping_status"]="LOCKED_DIRECT"; std["mapping_confidence"]="HIGH"; std["mapping_method"]="OECD_LOCKED_DIMENSION_DIRECT"
                parts.append(std); audit.append({"dataset_family":dataset_name,"locked_dataset":rr.get("source_dataset",""),"current_flow":flow.get("flow_id",""),"wbfd_mnemonic":rr["wbfd_mnemonic"],"rows":len(std),"status":"SUCCESS","key":key})
            except Exception as e:
                audit.append({"dataset_family":dataset_name,"locked_dataset":rr.get("source_dataset",""),"current_flow":flow.get("flow_id",""),"wbfd_mnemonic":rr["wbfd_mnemonic"],"rows":0,"status":"FAILED","key":key,"message":str(e)}); raise
            time.sleep(min(float(request_settings().get("sleep",5)),5.0))
    prod=pd.concat(parts,ignore_index=True,sort=False) if parts else pd.DataFrame()
    prod.to_csv(data_dir/"oecd_DIRECT_LOCKED_PRODUCTION.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(audit).to_csv(log_dir/"download_summary_latest.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(list(flows.values())).to_csv(log_dir/"resolved_dataflows_latest.csv",index=False,encoding="utf-8-sig")
    print(f"  OECD DIRECT LOCKED DONE: rows={len(prod):,}; codes={prod['wbfd_mnemonic'].nunique() if not prod.empty else 0}")
    return pd.DataFrame(audit)

def main():
    req = request_settings()
    cfg = source_settings(SOURCE)
    if os.environ.get("WBFD_ANNUAL_LOCKED_MODE", "").strip() == "1" and cfg.get("locked_only"):
        return _annual_locked_direct(cfg)
    flows = discover_latest_flows()
    wanted = set(cfg.get("locked_dataset_families") or [])
    if cfg.get("locked_only") and wanted:
        flows = {k:v for k,v in flows.items() if k in wanted}
        print("OECD selective annual mode: " + ", ".join(flows))
    selected_years = sorted({x["dataset_year"] for x in flows.values()})
    folder_vintage = str(selected_years[0]) if len(selected_years) == 1 else "mixed_latest"

    base = Path(f"oecd_data_{folder_vintage}_{run_stamp()}")
    data_dir = base / "data"
    structure_dir = base / "structure"
    log_dir = base / "logs"
    for p in (data_dir, structure_dir, log_dir):
        p.mkdir(parents=True, exist_ok=True)

    summary = []
    all_labels = []
    downloaded_frames = []
    metadata_dir = base / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    for number, (dataset_name, flow) in enumerate(flows.items(), start=1):
        print(f"\n[{number}/{len(flows)}] OECD {dataset_name}: {flow['flow_id']} v{flow['version']}")
        data_url, structure_url = build_urls(flow)
        stamp = datetime.now().isoformat(timespec="seconds")

        try:
            structure_file = save_structure(
                structure_url,
                structure_dir / f"{dataset_name}_{flow['dataset_year']}_structure.xml",
            )
            labels = extract_structure_labels(dataset_name, flow["dataset_year"], structure_file)
            if not labels.empty:
                labels.to_csv(
                    structure_dir / f"{dataset_name}_{flow['dataset_year']}_code_labels.csv",
                    index=False, encoding="utf-8-sig"
                )
                all_labels.append(labels)

            df = download_data(data_url)
            df["SOURCE_DATASET"] = dataset_name
            df["DATAFLOW_ID"] = flow["flow_id"]
            df["DATASET_YEAR"] = flow["dataset_year"]
            df["SDMX_VERSION"] = flow["version"]
            df["DOWNLOAD_TIMESTAMP"] = stamp
            out_file = data_dir / f"{dataset_name}_{flow['dataset_year']}_ALL.csv"
            df.to_csv(out_file, index=False, encoding="utf-8-sig")
            downloaded_frames.append(df.copy())

            years = pd.to_numeric(df.get("TIME_PERIOD"), errors="coerce") if "TIME_PERIOD" in df else pd.Series(dtype=float)
            countries = df["REF_AREA"].dropna().nunique() if "REF_AREA" in df else None
            summary.append({
                "dataset": dataset_name,
                "dataflow_id": flow["flow_id"],
                "dataset_year": flow["dataset_year"],
                "sdmx_version": flow["version"],
                "countries": countries,
                "requested_start_year": source_settings(SOURCE)["start_year"],
                "requested_end_year": source_settings(SOURCE).get("end_year"),
                "actual_min_year": years.min() if not years.empty else None,
                "actual_max_year": years.max() if not years.empty else None,
                "rows": len(df),
                "columns": len(df.columns),
                "status": "SUCCESS",
                "data_url": data_url,
                "structure_url": structure_url,
                "data_file": str(out_file),
            })
        except Exception as e:
            summary.append({
                "dataset": dataset_name,
                "dataflow_id": flow["flow_id"],
                "dataset_year": flow["dataset_year"],
                "sdmx_version": flow["version"],
                "status": "FAILED",
                "message": str(e),
                "data_url": data_url,
                "structure_url": structure_url,
            })

        if number < len(flows):
            time.sleep(req["sleep"])

    if all_labels:
        pd.concat(all_labels, ignore_index=True).to_csv(
            structure_dir / "all_code_labels_latest.csv", index=False, encoding="utf-8-sig"
        )

    # ------------------------------------------------------------------
    # Runtime mapping discovery is initial-build only. Annual mode still
    # saves the full source inventory but skips candidate generation/Excel.
    # ------------------------------------------------------------------
    series_inventory = build_series_inventory(downloaded_frames)
    series_inventory.to_csv(metadata_dir / "oecd_fiscal_series_inventory.csv", index=False, encoding="utf-8-sig")
    if os.environ.get("WBFD_SKIP_RUNTIME_MAPPING") == "1":
        print("\nOECD annual mode: FULL historical data downloaded; runtime mapping discovery/export SKIPPED", flush=True)
    else:
        mapping_candidates, mapping_best = build_mapping_candidates(series_inventory)
        mapping_candidates.to_csv(metadata_dir / "oecd_to_fiscal_mapping_candidates.csv", index=False, encoding="utf-8-sig")
        mapping_best.to_csv(metadata_dir / "oecd_to_fiscal_mapping_best.csv", index=False, encoding="utf-8-sig")
        runtime_xlsx = metadata_dir / "OECD_to_Fiscal_Survey_Mapping_RUNTIME.xlsx"
        mapping_stats = export_runtime_mapping_workbook(runtime_xlsx, series_inventory, mapping_candidates, mapping_best)
        print(f"\nOECD mapping: series={mapping_stats['series']:,}, mapped_codes={mapping_stats['mapped_codes']:,}, candidate_links={mapping_stats['candidate_links']:,}")
        print(f"OECD runtime mapping workbook: {runtime_xlsx.resolve()}")

    summary_df = pd.DataFrame(summary)
    summary_df.to_csv(log_dir / "download_summary_latest.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(list(flows.values())).to_csv(
        log_dir / "resolved_dataflows_latest.csv", index=False, encoding="utf-8-sig"
    )
    print(f"\nOECD output root: {base.resolve()}")
    return summary_df


if __name__ == "__main__":
    main()
