from __future__ import annotations

import os

import json
import re
import unicodedata
from pathlib import Path
from datetime import datetime

import pandas as pd

from common import (
    get, standardize, source_settings, filter_years,
    validate_observations, run_stamp
)

SOURCE = "ECLAC"
ECLAC_COLLECTOR_VERSION = "ECLAC_LOCKED_RECORDS_V2_20260923"
BASE = "https://api-cepalstat.cepal.org/cepalstat/api/v1"

FISCAL_MAPPING_RULES = [{'priority': 1,
  'category': 'Revenue',
  'code': 'GGREVTOTLCN',
  'variable': 'Total revenue',
  'include_any': ['government revenue', 'total revenue', 'ingresos totales', 'ingresos del gobierno', 'ingresos públicos', 'ingresos publicos'],
  'include_all': [],
  'exclude_any': ['tax', 'grant'],
  'decision': 'EXACT_CANDIDATE',
  'confidence': 'HIGH',
  'note': 'Exact known KIDB candidate; preserve institutional-scope/unit checks.'},
 {'priority': 2,
  'category': 'Revenue',
  'code': 'GGREVTAXTCN',
  'variable': 'Tax revenue (including social contributions)',
  'include_any': ['tax revenue', 'government taxes', 'ingresos tributarios', 'recaudación tributaria', 'recaudacion tributaria'],
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
  'include_any': ['taxes on goods and services', 'goods and services tax', 'tax on goods and services', 'impuestos sobre bienes y servicios'],
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
  'include_any': ['value added tax', 'value-added tax', 'vat', 'impuesto al valor agregado', 'impuesto sobre el valor añadido', 'iva'],
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
  'include_any': ['fuel excise', 'excise on fuel', 'fuel tax', 'petroleum excise', 'impuesto a los combustibles', 'impuestos sobre combustibles'],
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
  'include_any': ['other excise tax', 'other excise duties', 'otros impuestos selectivos', 'otros impuestos específicos', 'otros impuestos especificos'],
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
  'include_any': ['export tax', 'export taxes', 'export duty', 'export duties', 'impuestos a las exportaciones', 'derechos de exportación', 'derechos de exportacion'],
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
  'include_any': ['grants revenue', 'grant revenue', 'government grants received', 'grants received', 'donaciones', 'transferencias recibidas'],
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
  'include_any': ['social security benefit', 'social benefits', 'social protection', 'prestaciones de seguridad social'],
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
  'include_any': ['capital investment', 'government investment', 'public investment', 'gross fixed capital formation', 'inversión pública', 'inversion publica'],
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
  'include_any': ['other capital expenditure', 'capital transfers', 'other capital spending', 'otros gastos de capital'],
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
  'include_any': ['net external financing', 'external financing net', 'financiamiento externo', 'financiación externa', 'financiacion externa'],
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

# Discovery vocabulary is derived from all 64 rules; these are extra context terms.
FISCAL_CONTEXT_TERMS = [
    "fiscal", "government finance", "public finance", "government revenue",
    "tax revenue", "government expenditure", "public expenditure",
    "public debt", "government debt", "social contributions",
    "finanzas públicas", "finanzas publicas", "ingresos tributarios",
    "gasto público", "gasto publico", "deuda pública", "deuda publica",
    "sector público", "sector publico", "gobierno central", "gobierno general",
]

HARD_FALSE_POSITIVES = [
    "tax rate", "tax rates", "tasa impositiva", "alicuota", "alícuota",
    "tax expenditure", "tax expenditures", "gasto tributario", "gastos tributarios",
    "tax burden", "presión tributaria", "presion tributaria",
    "population covered", "cobertura de la población", "cobertura de la poblacion",
    "constant prices", "precios constantes",
    "gross capital formation", "formación bruta de capital", "formacion bruta de capital",
    "capital account", "cuenta de capital",
    "external debt total", "deuda externa total",  # not automatically government debt
    "household", "hogares",
]

def strip_accents(x):
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(x or ""))
        if not unicodedata.combining(c)
    )

def norm(x):
    return re.sub(r"\s+", " ", strip_accents(x).lower()).strip()

def safe_name(x):
    return re.sub(r"[^0-9A-Za-z_]+", "_", strip_accents(str(x))).strip("_")

def extract_cepalstat_observations(payload: dict) -> pd.DataFrame:
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise RuntimeError("CEPALSTAT response has no body object.")

    observations = body.get("data")
    dimensions = body.get("dimensions")
    if not isinstance(observations, list) or not observations:
        raise RuntimeError("CEPALSTAT body.data contains no observations.")
    if not isinstance(dimensions, list) or not dimensions:
        raise RuntimeError("CEPALSTAT body.dimensions is missing.")

    raw = pd.json_normalize(observations)

    for dim in dimensions:
        dim_id = dim.get("id")
        dim_name = dim.get("name") or f"dimension_{dim_id}"
        source_col = f"dim_{dim_id}"
        if source_col not in raw.columns:
            continue

        members = dim.get("members") or []
        member_name = {m.get("id"): m.get("name") for m in members}
        member_order = {m.get("id"): m.get("order") for m in members}

        label_col = safe_name(dim_name)
        raw[label_col] = raw[source_col].map(member_name)
        raw[f"{label_col}__ID"] = raw[source_col]
        raw[f"{label_col}__ORDER"] = raw[source_col].map(member_order)

    metadata = body.get("metadata") or {}
    raw["INDICATOR_ID"] = metadata.get("indicator_id")
    raw["INDICATOR_NAME"] = metadata.get("indicator_name")
    raw["INDICATOR_UNIT"] = metadata.get("unit")
    raw["INDICATOR_LAST_UPDATE"] = metadata.get("last_update")
    return raw

def find_dim_col(df, pattern):
    rgx = re.compile(pattern, re.I)
    return next(
        (c for c in df.columns
         if rgx.search(c) and not c.endswith(("__ID","__ORDER"))),
        None
    )

# ---------------------------------------------------------------------
# Thematic-tree discovery
# ---------------------------------------------------------------------
def thematic_tree():
    return get(f"{BASE}/thematic-tree").json()

def walk_tree(x, area_path=None):
    area_path = list(area_path or [])

    if isinstance(x, dict):
        # Identify area/category names that should be inherited by descendants.
        local_name = (
            x.get("name") or x.get("label") or x.get("title")
            or x.get("area_name") or x.get("area")
        )
        local_type = norm(x.get("type") or x.get("entity") or x.get("kind") or "")

        next_path = area_path
        if local_name and ("area" in local_type or "theme" in local_type or "category" in local_type):
            next_path = area_path + [str(local_name)]

        # Robustly detect indicator objects across API shape revisions.
        indicator_id = (
            x.get("indicator_id")
            or (x.get("id") if "indicator" in local_type else None)
        )
        indicator_name = (
            x.get("indicator_name")
            or (local_name if indicator_id is not None else None)
        )

        if indicator_id is not None and indicator_name:
            yield {
                "indicator_id": str(indicator_id),
                "indicator_name": str(indicator_name),
                "area_path": " > ".join(area_path),
                "raw_type": local_type,
            }

        # Some tree payloads use nested 'indicators' with simple id/name dicts.
        inds = x.get("indicators")
        if isinstance(inds, list):
            for ind in inds:
                if isinstance(ind, dict):
                    iid = ind.get("indicator_id") or ind.get("id")
                    name = ind.get("indicator_name") or ind.get("name") or ind.get("label") or ind.get("title")
                    if iid is not None and name:
                        yield {
                            "indicator_id": str(iid),
                            "indicator_name": str(name),
                            "area_path": " > ".join(next_path),
                            "raw_type": "indicator",
                        }

        for k,v in x.items():
            if k == "indicators":
                continue
            yield from walk_tree(v, next_path)

    elif isinstance(x, list):
        for v in x:
            yield from walk_tree(v, area_path)

def discover_indicator_catalog(payload):
    rows = list(walk_tree(payload))
    if not rows:
        raise RuntimeError(
            "Could not extract indicators from CEPALSTAT thematic-tree. "
            "Raw tree was saved for inspection."
        )
    df = pd.DataFrame(rows).drop_duplicates(["indicator_id","indicator_name"])
    return df

def fiscal_catalog_candidates(catalog):
    rule_terms = sorted(set(
        norm(x)
        for r in FISCAL_MAPPING_RULES
        for x in (r["include_any"] + r["include_all"])
        if x
    ))
    context_terms = [norm(x) for x in FISCAL_CONTEXT_TERMS]

    out = catalog.copy()
    out["search_text"] = (
        out["indicator_name"].fillna("").astype(str)
        + " | " + out["area_path"].fillna("").astype(str)
    ).map(norm)

    out["matched_terms"] = out["search_text"].map(
        lambda s: " | ".join(
            t for t in rule_terms + context_terms
            if len(t) >= 3 and t in s
        )
    )
    out["is_fiscal_candidate"] = out["matched_terms"].ne("")
    out["hard_false_positive"] = out["search_text"].map(
        lambda s: any(norm(t) in s for t in HARD_FALSE_POSITIVES)
    )

    return out[out["is_fiscal_candidate"] & ~out["hard_false_positive"]].copy()

# ---------------------------------------------------------------------
# Runtime mapping
# ---------------------------------------------------------------------
def score_mapping(rule, indicator_name, series_label, unit, area_path):
    indicator_text = norm(indicator_name)
    series_text = norm(series_label)
    combined = f"{indicator_text} | {series_text}"
    area_text = norm(area_path)
    unit_text = norm(unit)

    if any(norm(x) in combined for x in rule["exclude_any"] if x):
        return None
    if any(norm(x) in combined for x in HARD_FALSE_POSITIVES):
        return None

    required = [norm(x) for x in rule["include_all"] if x]
    if required and not all(x in combined for x in required):
        return None

    hits = [x for x in rule["include_any"] if norm(x) in combined]
    if not hits:
        return None

    # Phrase specificity dominates.
    score = 50 + max(len(norm(x).split()) for x in hits) * 20 + len(hits) * 5

    # Stronger if phrase is in the official indicator title, not only a classification.
    title_hits = [x for x in hits if norm(x) in indicator_text]
    if title_hits:
        score += 20

    # Government/fiscal area context increases confidence modestly.
    if any(norm(x) in area_text for x in [
        "fiscal", "public finance", "finanzas publicas", "government",
        "sector publico", "tax", "tribut"
    ]):
        score += 12

    # Unit clues. Percent-of-GDP series are candidates but not equivalent to LCU.
    if "%" in unit_text or "porcentaje" in unit_text or "percentage" in unit_text:
        score -= 5

    return score, " | ".join(hits)

def build_mapping(series_inventory, allowed_codes=None):
    rows = []
    allowed = {str(x).strip() for x in (allowed_codes or []) if str(x).strip()}
    active_rules = [r for r in FISCAL_MAPPING_RULES if not allowed or str(r.get("code","")).strip() in allowed]
    for _, rec in series_inventory.iterrows():
        for rule in active_rules:
            z = score_mapping(
                rule,
                rec.get("indicator_name",""),
                rec.get("series_label",""),
                rec.get("unit",""),
                rec.get("area_path",""),
            )
            if z is None:
                continue
            score, hits = z

            rows.append({
                "priority": rule["priority"],
                "category": rule["category"],
                "fiscal_survey_code": rule["code"],
                "fiscal_survey_variable": rule["variable"],
                "mapping_status": rule["decision"],
                "mapping_confidence": rule["confidence"],
                "rule_note": rule["note"],
                "eclac_indicator_id": rec.get("indicator_id"),
                "eclac_indicator_name": rec.get("indicator_name"),
                "eclac_series_label": rec.get("series_label"),
                "eclac_area_path": rec.get("area_path"),
                "unit": rec.get("unit"),
                "rows": rec.get("rows"),
                "countries": rec.get("countries"),
                "min_year": rec.get("min_year"),
                "max_year": rec.get("max_year"),
                "matched_phrases": hits,
                "mapping_score": score,
            })

    cand = pd.DataFrame(rows)
    if cand.empty:
        return cand, cand

    best = (
        cand.sort_values(
            ["fiscal_survey_code","mapping_score","countries","rows","priority"],
            ascending=[True,False,False,False,True]
        )
        .drop_duplicates("fiscal_survey_code", keep="first")
        .reset_index(drop=True)
    )
    return cand, best

def export_runtime_excel(path, catalog, discovered, series_inventory, candidates, best):
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
            "eclac_indicator_id":"ECLAC Indicator ID",
            "eclac_indicator_name":"ECLAC Indicator Name",
            "eclac_series_label":"ECLAC Series / Classification",
            "eclac_area_path":"ECLAC Area Path",
            "mapping_status":"Runtime Status",
            "mapping_confidence":"Runtime Confidence",
            "mapping_score":"Runtime Score",
            "matched_phrases":"Matched Phrases",
            "unit":"Unit",
            "rows":"Rows",
            "countries":"Countries",
            "min_year":"Min Year",
            "max_year":"Max Year",
        })
        cols = [
            "Fiscal Survey Code","ECLAC Indicator ID","ECLAC Indicator Name",
            "ECLAC Series / Classification","ECLAC Area Path",
            "Runtime Status","Runtime Confidence","Runtime Score",
            "Matched Phrases","Unit","Rows","Countries","Min Year","Max Year"
        ]
        full = full.merge(b[cols], on="Fiscal Survey Code", how="left")

    for c in [
        "ECLAC Indicator ID","ECLAC Indicator Name","ECLAC Series / Classification",
        "ECLAC Area Path","Runtime Status","Runtime Confidence","Runtime Score",
        "Matched Phrases","Unit","Rows","Countries","Min Year","Max Year"
    ]:
        if c not in full.columns:
            full[c] = ""

    full["Final Runtime Status"] = full["Runtime Status"].fillna("")
    full.loc[full["Final Runtime Status"].eq(""), "Final Runtime Status"] = "GAP"

    mapped = full[full["Final Runtime Status"] != "GAP"].copy()

    summary = pd.DataFrame([
        {"Metric":"Fiscal Survey variables","Value":len(FISCAL_MAPPING_RULES)},
        {"Metric":"CEPALSTAT indicators in thematic tree","Value":len(catalog)},
        {"Metric":"Fiscal indicator candidates discovered","Value":len(discovered)},
        {"Metric":"Downloaded fiscal series/classifications","Value":len(series_inventory)},
        {"Metric":"Runtime mapping candidate links","Value":len(candidates)},
        {"Metric":"Mapped Fiscal Survey codes","Value":mapped["Fiscal Survey Code"].nunique()},
        {"Metric":"Remaining GAP codes","Value":int((full["Final Runtime Status"]=="GAP").sum())},
    ])

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        full.to_excel(writer, sheet_name="ECLAC Full Mapping", index=False)
        mapped.to_excel(writer, sheet_name="ECLAC Mapped Only", index=False)
        series_inventory.to_excel(writer, sheet_name="Downloaded Series Inventory", index=False)
        candidates.to_excel(writer, sheet_name="Runtime Candidates", index=False)
        discovered.to_excel(writer, sheet_name="Discovered Fiscal Indicators", index=False)
        catalog.to_excel(writer, sheet_name="CEPALSTAT Indicator Catalog", index=False)
        rules_df.to_excel(writer, sheet_name="Embedded Mapping Rules", index=False)

        from copy import copy
        for ws in writer.book.worksheets:
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
                ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 45)

    return {
        "mapped_codes": int(mapped["Fiscal Survey Code"].nunique()),
        "gaps": int((full["Final Runtime Status"]=="GAP").sum()),
        "candidate_links": int(len(candidates)),
    }

# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def _cepalstat_body(payload):
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise RuntimeError("CEPALSTAT response has no body object.")
    return body


def _fetch_indicator_dimensions(indicator_id):
    """Lightweight metadata request used to translate LOCKED labels to member IDs."""
    payload = get(
        f"{BASE}/indicator/{indicator_id}/dimensions",
        params={"lang":"en", "format":"json", "in":1, "path":1},
    ).json()
    dims = _cepalstat_body(payload).get("dimensions") or []
    if not isinstance(dims, list) or not dims:
        raise RuntimeError(f"ECLAC {indicator_id}: dimensions endpoint returned no dimensions")
    return dims


def _is_country_dimension(dim):
    n = norm(dim.get("name") or "")
    return any(k in n for k in ["country", "pais", "país"])


def _is_year_dimension(dim):
    n = norm(dim.get("name") or "")
    return any(k in n for k in ["year", "years", "ano", "anos", "año", "años"])


def _active_members(dim):
    members = dim.get("members") or []
    active = [m for m in members if str(m.get("in", 1)) in {"1", "True", "true"}]
    return active or members


def _resolve_locked_member_ids(dimensions, locked_series_label):
    """
    Resolve one exact CEPALSTAT member per non-country/non-year classification
    dimension. If this cannot be done safely, return None so the caller falls
    back to the legacy full /data response rather than broadening the series.
    """
    label = str(locked_series_label or "").strip()
    components = [norm(x) for x in label.split("|") if norm(x)]
    selected = []
    detail = []

    class_dims = [d for d in dimensions if not _is_country_dimension(d) and not _is_year_dimension(d)]
    if not class_dims:
        return [], []

    # A blank series signature is safe only when every classification dimension
    # has a single active member.
    for dim in sorted(class_dims, key=lambda d: (d.get("position", 999), d.get("order", 999))):
        active = _active_members(dim)
        exact = []
        for m in active:
            mn = norm(m.get("name") or "")
            if mn and mn in components:
                exact.append(m)
        # Deduplicate exact member IDs.
        uniq = {str(m.get("id")): m for m in exact if m.get("id") is not None}
        exact = list(uniq.values())

        if len(exact) == 1:
            chosen = exact[0]
        elif len(exact) == 0 and len(active) == 1:
            # A dimension fixed to a single member does not need to appear in the
            # human-readable LOCKED label, but including it makes the API query exact.
            chosen = active[0]
        else:
            return None, [{"dimension":dim.get("name",""), "reason":"ambiguous_or_unmatched"}]

        selected.append(str(chosen.get("id")))
        detail.append({
            "dimension": str(dim.get("name") or ""),
            "dimension_id": str(dim.get("id") or ""),
            "member": str(chosen.get("name") or ""),
            "member_id": str(chosen.get("id") or ""),
        })

    return selected, detail


def _decode_records_payload(payload, dimensions):
    body = _cepalstat_body(payload)
    observations = body.get("data") or []
    if not isinstance(observations, list) or not observations:
        raise RuntimeError("CEPALSTAT /records returned zero observations")
    raw = pd.json_normalize(observations)
    for dim in dimensions:
        dim_id = dim.get("id")
        source_col = f"dim_{dim_id}"
        if source_col not in raw.columns:
            continue
        members = dim.get("members") or []
        member_name = {m.get("id"): m.get("name") for m in members}
        member_order = {m.get("id"): m.get("order") for m in members}
        label_col = safe_name(dim.get("name") or f"dimension_{dim_id}")
        raw[label_col] = raw[source_col].map(member_name)
        raw[f"{label_col}__ID"] = raw[source_col]
        raw[f"{label_col}__ORDER"] = raw[source_col].map(member_order)
    return raw


def _classification_key(raw, year_col, country_col):
    classification_cols = [
        c for c in raw.columns
        if c not in {"value","source_id","notes_ids","iso3",year_col,country_col}
        and not c.startswith("dim_")
        and not c.endswith(("__ID","__ORDER"))
        and not c.startswith("INDICATOR_")
    ]
    if classification_cols:
        return raw[classification_cols].astype("string").fillna("").agg(" | ".join, axis=1)
    return pd.Series("", index=raw.index, dtype="string")


def _legacy_full_indicator(iid, cache):
    """Correctness-preserving fallback. At most one full /data call per indicator."""
    if iid in cache:
        return cache[iid].copy()
    payload = get(f"{BASE}/indicator/{iid}/data").json()
    raw = extract_cepalstat_observations(payload)
    ycol = find_dim_col(raw, r"year|ano|anos|years")
    ccol = "iso3" if "iso3" in raw.columns else find_dim_col(raw, r"country|pais")
    if ycol is None or ccol is None or "value" not in raw.columns:
        raise RuntimeError(f"ECLAC {iid}: fallback /data missing country/year/value")
    raw = filter_years(raw, ycol, SOURCE)
    raw["_indicator_key"] = _classification_key(raw, ycol, ccol)
    std = standardize(
        raw, "ECLAC_CEPALSTAT", ccol, ycol, "value", "_indicator_key",
        "INDICATOR_UNIT" if "INDICATOR_UNIT" in raw.columns else None, None,
    )
    if "iso3" in raw.columns:
        std["country_code"] = raw["iso3"].astype(str).str.strip().str.upper().values
    std["INDICATOR_ID"] = str(iid)
    cache[iid] = std
    return std.copy()


def _annual_locked_direct(cfg):
    """
    Fast annual mode.

    Primary path:
      dimensions (small metadata) -> exact LOCKED member IDs -> /records?members=...
    Fallback path:
      legacy /data once per indicator, only if exact member resolution/request fails.
    """
    from common import load_locked_plan
    plan = load_locked_plan(SOURCE)
    reg = pd.DataFrame(plan.get("rows") or []).fillna("")
    if reg.empty:
        raise RuntimeError("ECLAC annual LOCKED plan has no rows")

    ren = {
        "ECLAC Indicator ID":"INDICATOR_ID",
        "ECLAC Series / Classification":"indicator_original",
        "Fiscal Survey Code":"wbfd_mnemonic",
        "Fiscal Survey Variable":"wbfd_variable",
        "Unit":"unit_original",
    }
    reg = reg.rename(columns={k:v for k,v in ren.items() if k in reg.columns and v not in reg.columns})
    for c in ["INDICATOR_ID","indicator_original","unit_original","wbfd_mnemonic","wbfd_variable"]:
        if c not in reg:
            reg[c] = ""
    reg["INDICATOR_ID"] = reg["INDICATOR_ID"].astype(str).str.replace(r"\.0$","",regex=True).str.strip()
    reg["indicator_original"] = reg["indicator_original"].astype(str).str.strip()
    reg["unit_original"] = reg["unit_original"].astype(str).str.strip()
    reg["wbfd_mnemonic"] = reg["wbfd_mnemonic"].astype(str).str.strip().str.upper()
    reg = reg[(reg.INDICATOR_ID != "") & (reg.wbfd_mnemonic != "")].copy()
    if reg.empty:
        raise RuntimeError("ECLAC LOCKED plan has zero indicator IDs")

    base = Path(f"eclac_data_latest_{run_stamp()}")
    data_dir, meta_dir, log_dir = base/"data", base/"metadata", base/"logs"
    for d in (data_dir, meta_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)
    reg.to_csv(meta_dir/"eclac_locked_registry_used.csv", index=False, encoding="utf-8-sig")

    parts = []
    audit = []
    dimension_cache = {}
    full_data_cache = {}

    # One API request per unique LOCKED source-series signature, not per Fiscal
    # Survey code. Multiple Fiscal Survey codes sharing one source series reuse it.
    sig_cols = ["INDICATOR_ID","indicator_original","unit_original"]
    signatures = reg[sig_cols].drop_duplicates().reset_index(drop=True)
    total = len(signatures)

    for n, sig in signatures.iterrows():
        iid = str(sig["INDICATOR_ID"])
        label = str(sig["indicator_original"] or "").strip()
        unit = str(sig["unit_original"] or "").strip()
        acquisition = "RECORDS_MEMBER_FILTER"
        member_ids = None
        member_detail = []
        fallback_reason = ""

        rr = reg[
            reg["INDICATOR_ID"].eq(iid)
            & reg["indicator_original"].eq(label)
            & reg["unit_original"].eq(unit)
        ][["wbfd_mnemonic","wbfd_variable"]].drop_duplicates()
        if rr.empty:
            continue

        # If the registry has multiple mappings for one indicator but no exact
        # series label, it is unsafe to use an unfiltered response.
        if not label and reg[reg["INDICATOR_ID"].eq(iid)]["wbfd_mnemonic"].nunique() > 1:
            raise RuntimeError(f"ECLAC {iid}: multiple LOCKED mappings but no exact series/classification signature")

        print(
            f"  ECLAC FAST [{n+1}/{total}] indicator={iid} "
            f"series={label[:80] if label else '[whole indicator]'}",
            flush=True,
        )

        try:
            if iid not in dimension_cache:
                dimension_cache[iid] = _fetch_indicator_dimensions(iid)
            dims = dimension_cache[iid]
            member_ids, member_detail = _resolve_locked_member_ids(dims, label)
            if member_ids is None:
                raise RuntimeError("LOCKED series label could not be resolved uniquely to CEPALSTAT dimension members")

            params = {"lang":"en", "format":"json"}
            if member_ids:
                params["members"] = ",".join(member_ids)
            response = get(f"{BASE}/indicator/{iid}/records", params=params).json()
            raw = _decode_records_payload(response, dims)

            ycol = find_dim_col(raw, r"year|ano|anos|years")
            country_dim = next((d for d in dims if _is_country_dimension(d)), None)
            country_label_col = safe_name(country_dim.get("name")) if country_dim else None
            ccol = country_label_col if country_label_col in raw.columns else ("iso3" if "iso3" in raw.columns else find_dim_col(raw, r"country|pais"))
            if ycol is None or ccol is None or "value" not in raw.columns:
                raise RuntimeError(f"ECLAC {iid}: /records missing country/year/value after dimension decode")
            raw = filter_years(raw, ycol, SOURCE)
            if raw.empty:
                raise RuntimeError(f"ECLAC {iid}: /records returned no observations in requested year range")

            raw["_indicator_key"] = label
            raw["INDICATOR_UNIT"] = unit
            std = standardize(raw, "ECLAC_CEPALSTAT", ccol, ycol, "value", "_indicator_key", "INDICATOR_UNIT", None)
            if "iso3" in raw.columns:
                std["country_code"] = raw["iso3"].astype(str).str.strip().str.upper().values
            std["INDICATOR_ID"] = iid
            std["ECLAC_MEMBER_FILTER"] = ",".join(member_ids or [])
            std["ECLAC_ACQUISITION_MODE"] = acquisition

        except Exception as e:
            # Preserve correctness if API/member semantics change. The fallback is
            # cached, so an indicator is downloaded in full at most once per run.
            fallback_reason = str(e)
            acquisition = "FULL_DATA_FALLBACK"
            print(f"    /records exact filter unavailable -> one-time /data fallback: {fallback_reason}", flush=True)
            full = _legacy_full_indicator(iid, full_data_cache)
            if label:
                std = full[full["indicator_original"].astype(str).str.strip().eq(label)].copy()
                if std.empty:
                    raise RuntimeError(f"ECLAC {iid}: fallback /data did not contain locked series: {label}")
            else:
                std = full.copy()
            if unit:
                # Unit is part of the LOCKED signature. Filter only when the
                # source response exposes units; otherwise retain the locked unit.
                if "unit_original" in std.columns and std["unit_original"].astype(str).str.strip().ne("").any():
                    unit_hit = std["unit_original"].astype(str).str.strip().str.lower().eq(unit.lower())
                    if unit_hit.any():
                        std = std.loc[unit_hit].copy()
                std["unit_original"] = unit
            std["ECLAC_MEMBER_FILTER"] = ""
            std["ECLAC_ACQUISITION_MODE"] = acquisition

        # Attach every Fiscal Survey mapping that intentionally uses this exact
        # source series. No fuzzy remapping occurs in annual mode.
        for _, maprow in rr.iterrows():
            hit = std.copy()
            hit["wbfd_mnemonic"] = str(maprow["wbfd_mnemonic"]).strip().upper()
            hit["wbfd_variable"] = str(maprow["wbfd_variable"]).strip()
            hit["mapping_status"] = "LOCKED_DIRECT"
            hit["mapping_confidence"] = "HIGH"
            hit["mapping_method"] = (
                "ECLAC_LOCKED_RECORDS_MEMBERS" if acquisition == "RECORDS_MEMBER_FILTER"
                else "ECLAC_LOCKED_FULL_DATA_FALLBACK"
            )
            parts.append(hit)

        audit.append({
            "indicator_id": iid,
            "indicator_original": label,
            "unit_original": unit,
            "fiscal_codes": " | ".join(sorted(rr["wbfd_mnemonic"].astype(str).unique())),
            "acquisition_mode": acquisition,
            "member_filter": ",".join(member_ids or []),
            "resolved_members": " | ".join(
                f"{x.get('dimension')}={x.get('member')}[{x.get('member_id')}]" for x in member_detail
            ),
            "rows_per_source_series": len(std),
            "status": "SUCCESS",
            "fallback_reason": fallback_reason,
        })

    prod = pd.concat(parts, ignore_index=True, sort=False) if parts else pd.DataFrame()
    if prod.empty:
        raise RuntimeError("ECLAC direct annual refresh produced zero LOCKED observations")
    prod = prod.drop_duplicates().reset_index(drop=True)
    prod.to_csv(data_dir/"eclac_DIRECT_LOCKED_PRODUCTION.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(audit).to_csv(log_dir/"download_summary_latest.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(audit).to_csv(meta_dir/"eclac_locked_request_audit.csv", index=False, encoding="utf-8-sig")
    print(
        f"  ECLAC FAST LOCKED DONE: rows={len(prod):,}; "
        f"codes={prod['wbfd_mnemonic'].nunique()}; "
        f"source_series={len(signatures)}; "
        f"full_data_fallbacks={sum(x['acquisition_mode']=='FULL_DATA_FALLBACK' for x in audit)}",
        flush=True,
    )
    return pd.DataFrame(audit)

def main():
    cfg = source_settings(SOURCE)
    annual_mode = os.environ.get("WBFD_ANNUAL_LOCKED_MODE", "").strip() == "1"
    if annual_mode:
        # Fail closed: annual production must NEVER enter thematic-tree discovery.
        # The LOCKED plan itself is validated inside _annual_locked_direct().
        print(f"  ECLAC collector version: {ECLAC_COLLECTOR_VERSION}", flush=True)
        print("  ECLAC ANNUAL LOCKED mode: thematic-tree discovery is HARD-DISABLED", flush=True)
        print("  ECLAC acquisition: /dimensions -> exact LOCKED members -> /records", flush=True)
        return _annual_locked_direct(cfg)

    base = Path(f"eclac_data_latest_{run_stamp()}")
    data_dir, meta_dir, log_dir = base/"data", base/"metadata", base/"logs"
    for p in (data_dir, meta_dir, log_dir):
        p.mkdir(parents=True, exist_ok=True)

    # 1) Full indicator catalog from official API.
    if os.environ.get("WBFD_ANNUAL_LOCKED_MODE", "").strip() == "1":
        raise RuntimeError("SAFETY STOP: annual ECLAC attempted broad thematic-tree discovery. Use the hardened annual-direct collector.")
    print("  ECLAC: loading full CEPALSTAT thematic tree ...")
    tree_payload = thematic_tree()
    (meta_dir/"thematic_tree_raw.json").write_text(
        json.dumps(tree_payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    catalog = discover_indicator_catalog(tree_payload)
    catalog.to_csv(
        meta_dir/"cepalstat_indicator_catalog.csv",
        index=False, encoding="utf-8-sig"
    )
    print(f"  ECLAC: thematic-tree indicators={len(catalog):,}")

    discovered = fiscal_catalog_candidates(catalog)

    # Preserve manually configured indicator IDs from common.py as explicit seeds.
    configured = cfg.get("indicators") or {}
    seed_rows = []
    for dataset_name, indicator_id in configured.items():
        hit = catalog[catalog["indicator_id"].astype(str).eq(str(indicator_id))]
        if not hit.empty:
            r = hit.iloc[0].to_dict()
            r["configured_name"] = dataset_name
            seed_rows.append(r)
        else:
            seed_rows.append({
                "indicator_id": str(indicator_id),
                "indicator_name": dataset_name,
                "area_path": "CONFIGURED_COMMON_PY",
                "raw_type": "configured",
                "configured_name": dataset_name,
            })

    if seed_rows:
        seeds = pd.DataFrame(seed_rows)
        discovered = pd.concat(
            [discovered, seeds],
            ignore_index=True, sort=False
        ).drop_duplicates("indicator_id", keep="first")

    # Annual refresh: download ONLY indicator IDs required by the LOCKED registry.
    if cfg.get("locked_only"):
        locked_ids = {str(v) for v in (cfg.get("indicators") or {}).values()}
        discovered = discovered[discovered["indicator_id"].astype(str).isin(locked_ids)].copy()
        print(f"  ECLAC selective annual mode: {len(discovered)} locked indicators selected")

    discovered.to_csv(
        meta_dir/"discovered_fiscal_indicators.csv",
        index=False, encoding="utf-8-sig"
    )
    print(f"  ECLAC: fiscal indicator candidates={len(discovered):,}")

    summary = []
    all_series = []

    # 2) Download every discovered fiscal indicator.
    for idx, rec in discovered.reset_index(drop=True).iterrows():
        indicator_id = str(rec["indicator_id"])
        indicator_name_tree = str(rec.get("indicator_name") or "")
        area_path = str(rec.get("area_path") or "")
        dataset_name = safe_name(indicator_name_tree)[:80] or f"indicator_{indicator_id}"

        print(
            f"  ECLAC [{idx+1:03d}/{len(discovered):03d}] "
            f"{indicator_id}: {indicator_name_tree[:80]}"
        )

        try:
            url = f"{BASE}/indicator/{indicator_id}/data"
            payload = get(url).json()

            (meta_dir/f"indicator_{indicator_id}_response.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            raw = extract_cepalstat_observations(payload)

            year_col = find_dim_col(raw, r"year|ano|anos|years")
            country_col = (
                "iso3" if "iso3" in raw.columns
                else find_dim_col(raw, r"country|pais")
            )
            value_col = "value"

            if year_col is None or country_col is None or value_col not in raw.columns:
                raise RuntimeError(
                    f"Required observation columns missing: "
                    f"country={country_col}, year={year_col}, value={value_col}"
                )

            raw = filter_years(raw, year_col, SOURCE)
            qc = validate_observations(
                raw, SOURCE, dataset_name,
                country_col, year_col, value_col
            )

            classification_cols = [
                c for c in raw.columns
                if c not in {
                    "value","source_id","notes_ids","iso3",
                    "INDICATOR_ID","INDICATOR_NAME","INDICATOR_UNIT",
                    "INDICATOR_LAST_UPDATE"
                }
                and not c.startswith("dim_")
                and not c.endswith(("__ID","__ORDER"))
                and c not in {year_col,country_col}
            ]

            if classification_cols:
                raw["_indicator_key"] = (
                    raw[classification_cols]
                    .astype("string").fillna("")
                    .agg(" | ".join, axis=1)
                )
            else:
                raw["_indicator_key"] = raw["INDICATOR_NAME"]

            # Runtime series/classification inventory with actual observation coverage.
            tmp = raw.copy()
            tmp["_year_num"] = pd.to_numeric(tmp[year_col], errors="coerce")
            series_cov = (
                tmp.groupby(
                    ["_indicator_key","INDICATOR_NAME","INDICATOR_UNIT"],
                    dropna=False
                )
                .agg(
                    rows=(value_col,"size"),
                    countries=(country_col, lambda x: x.dropna().nunique()),
                    min_year=("_year_num","min"),
                    max_year=("_year_num","max"),
                )
                .reset_index()
            )
            for _, srow in series_cov.iterrows():
                all_series.append({
                    "indicator_id": indicator_id,
                    "indicator_name": srow["INDICATOR_NAME"] or indicator_name_tree,
                    "series_label": srow["_indicator_key"],
                    "area_path": area_path,
                    "unit": srow["INDICATOR_UNIT"],
                    "rows": srow["rows"],
                    "countries": srow["countries"],
                    "min_year": srow["min_year"],
                    "max_year": srow["max_year"],
                })

            std = standardize(
                raw, "ECLAC_CEPALSTAT",
                country_col=country_col,
                year_col=year_col,
                value_col=value_col,
                indicator_col="_indicator_key",
                unit_col="INDICATOR_UNIT",
            )

            stamp = datetime.now().isoformat(timespec="seconds")
            for frame in (raw,std):
                frame["SOURCE_DATASET"] = indicator_name_tree
                frame["INDICATOR_ID"] = indicator_id
                frame["CEPALSTAT_AREA_PATH"] = area_path
                frame["DOWNLOAD_TIMESTAMP"] = stamp

            raw_file = data_dir/f"indicator_{indicator_id}_raw.csv"
            std_file = data_dir/f"indicator_{indicator_id}_standardized.csv"
            raw.to_csv(raw_file,index=False,encoding="utf-8-sig")
            std.to_csv(std_file,index=False,encoding="utf-8-sig")

            summary.append({
                "dataset":indicator_name_tree,
                "indicator_id":indicator_id,
                **qc,
                "status":"SUCCESS",
                "data_file":str(std_file),
            })
            print(
                f"    SUCCESS rows={qc['rows']:,} "
                f"countries={qc['countries']} "
                f"years={qc['actual_min_year']}-{qc['actual_max_year']}"
            )

        except Exception as e:
            print(f"    FAILED: {e}")
            summary.append({
                "dataset":indicator_name_tree,
                "indicator_id":indicator_id,
                "rows":0,
                "status":"FAILED",
                "message":str(e),
            })

    summary_df = pd.DataFrame(summary)
    summary_df.to_csv(
        log_dir/"download_summary_latest.csv",
        index=False, encoding="utf-8-sig"
    )

    series_inventory = pd.DataFrame(all_series)
    series_inventory.to_csv(
        meta_dir/"downloaded_fiscal_series_inventory.csv",
        index=False, encoding="utf-8-sig"
    )

    # 3) Mapping discovery belongs only to the initial mapping-build stage.
    # Annual refresh downloads the full history, then the parent runner applies
    # the read-only LOCKED registry.
    if os.environ.get("WBFD_SKIP_RUNTIME_MAPPING") == "1":
        print("\n  ECLAC annual mode: FULL historical data downloaded; runtime mapping discovery/export SKIPPED", flush=True)
    else:
        candidates, best = build_mapping(series_inventory, cfg.get("locked_fiscal_codes") if cfg.get("locked_only") else None)
        candidates.to_csv(meta_dir/"eclac_to_fiscal_mapping_candidates.csv", index=False, encoding="utf-8-sig")
        best.to_csv(meta_dir/"eclac_to_fiscal_mapping_best.csv", index=False, encoding="utf-8-sig")
        runtime_xlsx = meta_dir/"ECLAC_CEPALSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx"
        stats = export_runtime_excel(runtime_xlsx, catalog, discovered, series_inventory, candidates, best)
        print(f"\n  ECLAC mapping: mapped Fiscal Survey codes={stats['mapped_codes']}, gaps={stats['gaps']}, candidate_links={stats['candidate_links']}")
        print(f"  ECLAC runtime mapping workbook: {runtime_xlsx}")
    print(f"\nECLAC output root: {base.resolve()}")

    return summary_df

if __name__ == "__main__":
    main()
