from __future__ import annotations
import argparse,subprocess,sys,traceback,shutil,os,json,importlib.util
from datetime import datetime
from pathlib import Path
import pandas as pd
import locked_registry as lock
SOURCES={'OECD':'oecd_main.py','ADB':'adb_main.py','AFDB':'afdb_aih_main.py','ECLAC':'eclac_main.py','EUROSTAT':'eurostat_main.py','IDB':'idb_main.py'}
PATTERNS={'OECD':['oecd_data_*'],'ADB':['adb_fiscal_data_latest_*'],'AFDB':['afdb_fiscal_data_latest_*'],'ECLAC':['eclac_data_latest_*'],'EUROSTAT':['eurostat_data_latest_*'],'IDB':['idb_lmw_fiscal_survey_*']}
DIRECT={'OECD':'data/oecd_DIRECT_LOCKED_PRODUCTION.csv','ADB':'data/adb_DIRECT_LOCKED_PRODUCTION.csv','ECLAC':'data/eclac_DIRECT_LOCKED_PRODUCTION.csv','EUROSTAT':'data/eurostat_DIRECT_LOCKED_PRODUCTION.csv','IDB':'data/03_idb_PRODUCTION_2007_latest_with_fiscal_survey_variables.csv'}
BUNDLED_EXPECTED_CODES={'OECD':23,'ADB':22,'AFDB':9,'ECLAC':28,'EUROSTAT':15,'IDB':9}
def newest(base,pats):
 xs=[]
 for pat in pats: xs += [p for p in base.glob(pat) if p.is_dir()]
 return max(xs,key=lambda p:p.stat().st_mtime) if xs else None


def _norm_text(x):
 return '' if pd.isna(x) else str(x).strip()

def _adb_search_roots(base):
 """Small, bounded set of folders where an earlier ADB mapping build may live."""
 roots=[]
 for q in [base, base.parent, base.parent.parent]:
  try: q=Path(q).resolve()
  except Exception: continue
  if q not in roots: roots.append(q)
 return roots


def _adb_mapping_workbook_candidates(base):
 """Find reviewed/runtime ADB mapping workbooks, including sibling pipeline folders."""
 out=[]
 def add(p):
  p=Path(p)
  if p.exists() and p.is_file() and p not in out: out.append(p)

 # Current package first.
 for p in [
  base/'mapping_tables'/'ADB_to_Fiscal_Survey_Mapping.xlsx',
  base/'ADB_to_Fiscal_Survey_Mapping.xlsx',
  base/'mapping_tables'/'ADB_KIDB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx',
 ]: add(p)
 for root in sorted([p for p in base.glob('adb_fiscal_data_latest_*') if p.is_dir()],key=lambda p:p.stat().st_mtime,reverse=True):
  add(root/'metadata'/'ADB_KIDB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx')
  add(root/'metadata'/'ADB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx')

 # The user often extracts a fixed package beside the previous package in Downloads.
 # Search only likely fiscal-pipeline siblings, not the whole drive.
 for parent in _adb_search_roots(base)[1:]:
  try:
   siblings=[]
   for pat in ['fiscal_pipeline*','fiscal_mapping*']:
    siblings.extend([d for d in parent.glob(pat) if d.is_dir()])
   siblings=sorted(set(siblings),key=lambda d:d.stat().st_mtime,reverse=True)[:30]
   for d in siblings:
    for pat in [
     '**/ADB_KIDB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx',
     '**/ADB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx',
     '**/ADB_to_Fiscal_Survey_Mapping.xlsx',
    ]:
     for q in sorted(d.glob(pat),key=lambda x:x.stat().st_mtime,reverse=True)[:5]: add(q)
  except Exception:
   pass
 return out


def _adb_mapping_csv_candidates(base):
 """Fallback source for exact ADB selections when no runtime workbook is available."""
 out=[]
 def add(p):
  p=Path(p)
  if p.exists() and p.is_file() and p not in out: out.append(p)
 for root0 in _adb_search_roots(base):
  candidates=[]
  try:
   if root0==base:
    candidates += list(base.glob('adb_fiscal_data_latest_*/metadata/adb_to_fiscal_mapping_best.csv'))
   else:
    for pat in ['fiscal_pipeline*','fiscal_mapping*']:
     for d in [x for x in root0.glob(pat) if x.is_dir()][:30]:
      candidates += list(d.glob('**/adb_to_fiscal_mapping_best.csv'))
  except Exception:
   pass
  for q in sorted(set(candidates),key=lambda x:x.stat().st_mtime,reverse=True)[:20]: add(q)
 return out


def _read_adb_exact_map(path):
 """Read one selected ADB source series per Fiscal Survey code from a mapping workbook."""
 xl=pd.ExcelFile(path)
 pref=['ADB Mapped Only','LOCKED Mapping','Mapped Only','ADB Full Mapping']
 sh=next((x for x in pref if x in xl.sheet_names),None)
 if sh is None: sh=next((x for x in xl.sheet_names if 'mapped only' in x.lower()),None)
 if sh is None: return pd.DataFrame()
 x=pd.read_excel(path,sheet_name=sh,dtype=str).fillna('')
 ren={
  'Fiscal Survey Code':'wbfd_mnemonic','fiscal_survey_code':'wbfd_mnemonic','WBFD Mnemonic':'wbfd_mnemonic',
  'Fiscal Survey Variable':'wbfd_variable','fiscal_survey_variable':'wbfd_variable',
  'ADB Flow':'SOURCE_DATAFLOW','flow':'SOURCE_DATAFLOW','Source Dataflow':'SOURCE_DATAFLOW',
  'ADB Indicator Code':'ADB_INDICATOR_CODE','adb_indicator_code':'ADB_INDICATOR_CODE','Indicator Code':'ADB_INDICATOR_CODE',
  'ADB Indicator Label':'indicator_original','adb_indicator_label':'indicator_original','Source Indicator':'indicator_original',
  'Unit':'unit_original','ADB Unit':'unit_original',
 }
 x=x.rename(columns={k:v for k,v in ren.items() if k in x.columns and v not in x.columns})
 for c in ['wbfd_mnemonic','wbfd_variable','SOURCE_DATAFLOW','ADB_INDICATOR_CODE','indicator_original','unit_original']:
  if c not in x: x[c]=''
 # Remove explicit GAP/rejected rows if present.
 for c in ['Final Runtime Status','Final Mapping Status']:
  if c in x.columns:
   v=x[c].astype(str).str.strip().str.upper()
   x=x[~v.isin(['','GAP','REJECTED','DROP','NO'])].copy()
 x['wbfd_mnemonic']=x['wbfd_mnemonic'].astype(str).str.strip().str.upper()
 x['SOURCE_DATAFLOW']=x['SOURCE_DATAFLOW'].astype(str).str.strip()
 x['ADB_INDICATOR_CODE']=x['ADB_INDICATOR_CODE'].astype(str).str.strip().str.replace(r'\.0$','',regex=True)
 x['indicator_original']=x['indicator_original'].astype(str).str.strip()
 x=x[(x.wbfd_mnemonic!='')&(x.wbfd_mnemonic!='NAN')&(x.SOURCE_DATAFLOW!='')&(x.ADB_INDICATOR_CODE!='')].copy()
 # Runtime 'ADB Mapped Only' is already one source series per Fiscal Survey code.
 # If a workbook contains duplicates, prefer highest mapping score deterministically.
 if 'Mapping Score' in x.columns:
  x['_score']=pd.to_numeric(x['Mapping Score'],errors='coerce').fillna(-1e18)
  x=x.sort_values(['wbfd_mnemonic','_score'],ascending=[True,False]).drop(columns=['_score'])
 x=x.drop_duplicates('wbfd_mnemonic',keep='first')
 return x[['wbfd_mnemonic','wbfd_variable','SOURCE_DATAFLOW','ADB_INDICATOR_CODE','indicator_original','unit_original']].drop_duplicates()


def _read_adb_best_csv(path):
 """Recover the top ADB source series per Fiscal Survey code from cached best-mapping CSV."""
 x=pd.read_csv(path,dtype=str,low_memory=False).fillna('')
 ren={
  'fiscal_survey_code':'wbfd_mnemonic','Fiscal Survey Code':'wbfd_mnemonic',
  'fiscal_survey_variable':'wbfd_variable','Fiscal Survey Variable':'wbfd_variable',
  'flow':'SOURCE_DATAFLOW','ADB Flow':'SOURCE_DATAFLOW',
  'adb_indicator_code':'ADB_INDICATOR_CODE','ADB Indicator Code':'ADB_INDICATOR_CODE',
  'adb_indicator_label':'indicator_original','ADB Indicator Label':'indicator_original',
 }
 x=x.rename(columns={k:v for k,v in ren.items() if k in x.columns and v not in x.columns})
 for c in ['wbfd_mnemonic','wbfd_variable','SOURCE_DATAFLOW','ADB_INDICATOR_CODE','indicator_original','unit_original']:
  if c not in x: x[c]=''
 x['wbfd_mnemonic']=x['wbfd_mnemonic'].astype(str).str.strip().str.upper()
 x['SOURCE_DATAFLOW']=x['SOURCE_DATAFLOW'].astype(str).str.strip()
 x['ADB_INDICATOR_CODE']=x['ADB_INDICATOR_CODE'].astype(str).str.strip().str.replace(r'\.0$','',regex=True)
 x=x[(x.wbfd_mnemonic!='')&(x.wbfd_mnemonic!='NAN')&(x.SOURCE_DATAFLOW!='')&(x.ADB_INDICATOR_CODE!='')].copy()
 if x.empty: return x
 x['_score']=pd.to_numeric(x.get('mapping_score',''),errors='coerce').fillna(-1e18)
 x['_priority']=pd.to_numeric(x.get('priority',''),errors='coerce').fillna(999999)
 # This recreates the same selection used by export_runtime_mapping_workbook():
 # highest mapping_score, then lower Fiscal Survey rule priority.
 x=x.sort_values(['wbfd_mnemonic','_score','_priority'],ascending=[True,False,True])
 x=x.drop_duplicates('wbfd_mnemonic',keep='first')
 return x[['wbfd_mnemonic','wbfd_variable','SOURCE_DATAFLOW','ADB_INDICATOR_CODE','indicator_original','unit_original']].drop_duplicates()


def _write_adb_registry(regdir, exact, note, old_path=None):
 """Write a canonical ADB LOCKED registry from already-selected exact source signatures."""
 exact=exact.copy().fillna('')
 for c in ['wbfd_mnemonic','wbfd_variable','SOURCE_DATAFLOW','ADB_INDICATOR_CODE','indicator_original','unit_original']:
  if c not in exact: exact[c]=''
 exact['wbfd_mnemonic']=exact['wbfd_mnemonic'].astype(str).str.strip().str.upper()
 exact['SOURCE_DATAFLOW']=exact['SOURCE_DATAFLOW'].astype(str).str.strip()
 exact['ADB_INDICATOR_CODE']=exact['ADB_INDICATOR_CODE'].astype(str).str.strip().str.replace(r'\.0$','',regex=True)
 exact=exact[(exact.wbfd_mnemonic!='')&(exact.SOURCE_DATAFLOW!='')&(exact.ADB_INDICATOR_CODE!='')].copy()
 exact=exact.drop_duplicates('wbfd_mnemonic',keep='first')
 if exact.empty: raise RuntimeError('Could not reconstruct any exact ADB mappings.')
 exact['institution']='ADB'; exact['registry_status']='LOCKED'; exact['registry_note']=note
 for c in lock.AUDIT_FIELDS['ADB']:
  if c not in exact: exact[c]=''
 exact['signature']=exact.apply(lambda r:lock.signature_row(r,'ADB'),axis=1)
 cols=['institution','signature','registry_status','registry_note','wbfd_mnemonic','wbfd_variable']+lock.AUDIT_FIELDS['ADB']
 out=exact[list(dict.fromkeys(cols))].drop_duplicates().reset_index(drop=True)
 regdir.mkdir(parents=True,exist_ok=True)
 p=regdir/'ADB_LOCKED.csv'
 if old_path is not None and Path(old_path).exists():
  stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
  backup=regdir/f'ADB_LOCKED_pre_rebuild_{stamp}.csv'
  shutil.copy2(old_path,backup)
  print(f'  ADB old registry backup: {backup}',flush=True)
 out.to_csv(p,index=False,encoding='utf-8-sig')
 try: out.to_excel(regdir/'ADB_LOCKED.xlsx',index=False)
 except Exception: pass
 return out


def ensure_adb_registry_exact(base,regdir):
 """
 Guarantee a usable exact ADB registry before annual refresh.

 Recovery order:
   1) existing usable ADB_LOCKED.csv;
   2) preserve its locked Fiscal Survey codes and recover flow/code from a mapping workbook;
   3) if the registry is empty/broken, rebuild from the newest prior ADB 'Mapped Only' workbook;
   4) last fallback: rebuild the same top-per-Fiscal-code selection from cached adb_to_fiscal_mapping_best.csv.

 Broad ADB dataflow discovery is never performed here.
 """
 p=regdir/'ADB_LOCKED.csv'
 reg=pd.DataFrame()
 if p.exists():
  try: reg=pd.read_csv(p,dtype=str,low_memory=False).fillna('')
  except Exception as e: print(f'  WARNING: could not read existing ADB_LOCKED.csv: {e}',flush=True)
 for c in ['SOURCE_DATAFLOW','ADB_INDICATOR_CODE','wbfd_mnemonic']:
  if c not in reg: reg[c]=''
 reg['wbfd_mnemonic']=reg['wbfd_mnemonic'].astype(str).str.strip().str.upper() if 'wbfd_mnemonic' in reg else ''
 valid=(reg['wbfd_mnemonic'].ne('') & reg['wbfd_mnemonic'].ne('NAN') & reg['SOURCE_DATAFLOW'].astype(str).str.strip().ne('') & reg['ADB_INDICATOR_CODE'].astype(str).str.strip().ne('')) if len(reg) else pd.Series(dtype=bool)
 if len(reg)>0 and valid.all():
  print(f"ADB LOCKED registry: exact source signatures OK ({len(reg)} rows, {reg['SOURCE_DATAFLOW'].nunique()} flows)",flush=True)
  return reg

 locked_codes=set(reg.loc[reg['wbfd_mnemonic'].ne('') & reg['wbfd_mnemonic'].ne('NAN'),'wbfd_mnemonic']) if len(reg) else set()
 print(f"ADB LOCKED registry needs recovery: rows={len(reg)}, usable_exact_rows={int(valid.sum()) if len(valid) else 0}, locked_codes={len(locked_codes)}",flush=True)

 # Prefer a previous runtime/review workbook because 'ADB Mapped Only' is exactly
 # the one-source-series-per-Fiscal-code selection used by the old pipeline.
 for wb in _adb_mapping_workbook_candidates(base):
  try: exact=_read_adb_exact_map(wb)
  except Exception as e:
   print(f'  ADB recovery skip workbook {wb}: {e}',flush=True); continue
  if locked_codes: exact=exact[exact['wbfd_mnemonic'].isin(locked_codes)].copy()
  if not exact.empty and (not locked_codes or locked_codes.issubset(set(exact['wbfd_mnemonic']))):
   note=('ADB exact source signatures reconstructed from prior mapping workbook; '
         'no broad annual discovery performed. Existing locked Fiscal Survey choices were preserved.' if locked_codes
         else 'ADB LOCKED registry bootstrapped from prior ADB Mapped Only selection; no broad annual discovery performed.')
   out=_write_adb_registry(regdir,exact,note,p if p.exists() else None)
   print(f"ADB LOCKED registry AUTO-RECOVERED from workbook: {wb}",flush=True)
   print(f"  locked codes={out['wbfd_mnemonic'].nunique()}, exact rows={len(out)}, flows={out['SOURCE_DATAFLOW'].nunique()}",flush=True)
   return out

 # Fallback to the cached best-mapping CSV and recreate exactly the same
 # top-per-Fiscal-code selection used to construct 'ADB Mapped Only'.
 for csv in _adb_mapping_csv_candidates(base):
  try: exact=_read_adb_best_csv(csv)
  except Exception as e:
   print(f'  ADB recovery skip csv {csv}: {e}',flush=True); continue
  if locked_codes: exact=exact[exact['wbfd_mnemonic'].isin(locked_codes)].copy()
  if not exact.empty and (not locked_codes or locked_codes.issubset(set(exact['wbfd_mnemonic']))):
   note=('ADB exact source signatures reconstructed from cached adb_to_fiscal_mapping_best.csv; '
         'no broad annual discovery performed.')
   out=_write_adb_registry(regdir,exact,note,p if p.exists() else None)
   print(f"ADB LOCKED registry AUTO-RECOVERED from cached mapping CSV: {csv}",flush=True)
   print(f"  locked codes={out['wbfd_mnemonic'].nunique()}, exact rows={len(out)}, flows={out['SOURCE_DATAFLOW'].nunique()}",flush=True)
   return out

 searched=[str(x) for x in _adb_mapping_workbook_candidates(base)] + [str(x) for x in _adb_mapping_csv_candidates(base)]
 raise RuntimeError(
  'ADB_LOCKED.csv is empty/broken and no prior exact ADB mapping artifact could be recovered. '
  'The annual runner intentionally will NOT start 65-dataflow discovery. '
  'Copy the previous adb_fiscal_data_latest_*/metadata/ADB_KIDB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx '
  'into this package (or mapping_tables/) and rerun. Searched artifacts: '+('; '.join(searched) if searched else 'none found')
 )


def _eclac_mapping_workbook_candidates(base):
 """Find prior ECLAC reviewed/runtime mapping workbooks without calling CEPALSTAT discovery."""
 out=[]
 def add(p):
  p=Path(p)
  if p.exists() and p.is_file() and p not in out: out.append(p)
 for p in [
  base/'mapping_tables'/'ECLAC_to_Fiscal_Survey_Mapping.xlsx',
  base/'ECLAC_to_Fiscal_Survey_Mapping.xlsx',
  base/'mapping_tables'/'ECLAC_CEPALSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx',
 ]: add(p)
 for root in sorted([p for p in base.glob('eclac_data_latest_*') if p.is_dir()],key=lambda p:p.stat().st_mtime,reverse=True):
  add(root/'metadata'/'ECLAC_CEPALSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx')
 for parent in _adb_search_roots(base)[1:]:
  try:
   siblings=[]
   for pat in ['fiscal_pipeline*','fiscal_mapping*']:
    siblings.extend([d for d in parent.glob(pat) if d.is_dir()])
   siblings=sorted(set(siblings),key=lambda d:d.stat().st_mtime,reverse=True)[:30]
   for d in siblings:
    for pat in [
     '**/ECLAC_CEPALSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx',
     '**/ECLAC_to_Fiscal_Survey_Mapping.xlsx',
    ]:
     for q in sorted(d.glob(pat),key=lambda x:x.stat().st_mtime,reverse=True)[:5]: add(q)
  except Exception:
   pass
 return out


def _eclac_mapping_csv_candidates(base):
 """Find cached ECLAC best-mapping CSVs in current or nearby previous packages."""
 out=[]
 def add(p):
  p=Path(p)
  if p.exists() and p.is_file() and p not in out: out.append(p)
 for root0 in _adb_search_roots(base):
  candidates=[]
  try:
   if root0==base:
    candidates += list(base.glob('eclac_data_latest_*/metadata/eclac_to_fiscal_mapping_best.csv'))
   else:
    for pat in ['fiscal_pipeline*','fiscal_mapping*']:
     for d in [x for x in root0.glob(pat) if x.is_dir()][:30]:
      candidates += list(d.glob('**/eclac_to_fiscal_mapping_best.csv'))
  except Exception:
   pass
  for q in sorted(set(candidates),key=lambda x:x.stat().st_mtime,reverse=True)[:20]: add(q)
 return out



def _eclac_series_inventory_candidates(base):
 """Find cached ECLAC downloaded-series inventories; these allow offline mapping recovery."""
 out=[]
 def add(p):
  p=Path(p)
  if p.exists() and p.is_file() and p not in out: out.append(p)
 for root0 in _adb_search_roots(base):
  candidates=[]
  try:
   if root0==base:
    candidates += list(base.glob('eclac_data_latest_*/metadata/downloaded_fiscal_series_inventory.csv'))
   else:
    for pat in ['fiscal_pipeline*','fiscal_mapping*']:
     for d in [x for x in root0.glob(pat) if x.is_dir()][:30]:
      candidates += list(d.glob('**/downloaded_fiscal_series_inventory.csv'))
  except Exception:
   pass
  for q in sorted(set(candidates),key=lambda x:x.stat().st_mtime,reverse=True)[:20]: add(q)
 return out


def _read_eclac_from_series_inventory(path, base):
 """Recreate the old ECLAC runtime 'best' mapping offline from a cached series inventory."""
 inv=pd.read_csv(path,low_memory=False).fillna('')
 required={'indicator_id','series_label'}
 if inv.empty or not required.issubset(inv.columns):
  return pd.DataFrame()
 helper=base/'eclac_main.py'
 if not helper.exists():
  return pd.DataFrame()
 spec=importlib.util.spec_from_file_location('_eclac_mapping_recovery_helper',helper)
 mod=importlib.util.module_from_spec(spec)
 spec.loader.exec_module(mod)
 _,best=mod.build_mapping(inv)
 if best is None or best.empty:
  return pd.DataFrame()
 x=best.rename(columns={
  'fiscal_survey_code':'wbfd_mnemonic',
  'fiscal_survey_variable':'wbfd_variable',
  'eclac_indicator_id':'INDICATOR_ID',
  'eclac_series_label':'indicator_original',
  'unit':'unit_original',
 }).copy()
 for c in ['wbfd_mnemonic','wbfd_variable','INDICATOR_ID','indicator_original','unit_original']:
  if c not in x: x[c]=''
 x['wbfd_mnemonic']=x['wbfd_mnemonic'].astype(str).str.strip().str.upper()
 x['INDICATOR_ID']=x['INDICATOR_ID'].astype(str).str.replace(r'\.0$','',regex=True).str.strip()
 x['indicator_original']=x['indicator_original'].astype(str).str.strip()
 x['unit_original']=x['unit_original'].astype(str).str.strip()
 x=x[(x.wbfd_mnemonic!='')&(x.INDICATOR_ID!='')].copy().drop_duplicates('wbfd_mnemonic',keep='first')
 return x[['wbfd_mnemonic','wbfd_variable','INDICATOR_ID','indicator_original','unit_original']].drop_duplicates()

def _read_eclac_exact_map(path):
 """Read the one selected ECLAC source series per Fiscal Survey code from a mapping workbook."""
 xl=pd.ExcelFile(path)
 pref=['ECLAC Mapped Only','LOCKED Mapping','Mapped Only','ECLAC Full Mapping']
 sh=next((x for x in pref if x in xl.sheet_names),None)
 if sh is None: sh=next((x for x in xl.sheet_names if 'mapped only' in x.lower()),None)
 if sh is None: return pd.DataFrame()
 x=pd.read_excel(path,sheet_name=sh,dtype=str).fillna('')
 ren={
  'Fiscal Survey Code':'wbfd_mnemonic','fiscal_survey_code':'wbfd_mnemonic','WBFD Mnemonic':'wbfd_mnemonic',
  'Fiscal Survey Variable':'wbfd_variable','fiscal_survey_variable':'wbfd_variable',
  'ECLAC Indicator ID':'INDICATOR_ID','eclac_indicator_id':'INDICATOR_ID','Indicator ID':'INDICATOR_ID',
  'ECLAC Series / Classification':'indicator_original','eclac_series_label':'indicator_original','Source Indicator':'indicator_original',
  'Unit':'unit_original','unit':'unit_original',
 }
 x=x.rename(columns={k:v for k,v in ren.items() if k in x.columns and v not in x.columns})
 for c in ['wbfd_mnemonic','wbfd_variable','INDICATOR_ID','indicator_original','unit_original']:
  if c not in x: x[c]=''
 for c in ['Final Runtime Status','Final Mapping Status']:
  if c in x.columns:
   v=x[c].astype(str).str.strip().str.upper()
   x=x[~v.isin(['','GAP','REJECTED','DROP','NO'])].copy()
 x['wbfd_mnemonic']=x['wbfd_mnemonic'].astype(str).str.strip().str.upper()
 x['INDICATOR_ID']=x['INDICATOR_ID'].astype(str).str.replace(r'\.0$','',regex=True).str.strip()
 x['indicator_original']=x['indicator_original'].astype(str).str.strip()
 x['unit_original']=x['unit_original'].astype(str).str.strip()
 x=x[(x.wbfd_mnemonic!='')&(x.wbfd_mnemonic!='NAN')&(x.INDICATOR_ID!='')].copy()
 score_col=next((c for c in ['Runtime Score','Mapping Score','mapping_score'] if c in x.columns),None)
 if score_col:
  x['_score']=pd.to_numeric(x[score_col],errors='coerce').fillna(-1e18)
  x=x.sort_values(['wbfd_mnemonic','_score'],ascending=[True,False]).drop(columns=['_score'])
 x=x.drop_duplicates('wbfd_mnemonic',keep='first')
 return x[['wbfd_mnemonic','wbfd_variable','INDICATOR_ID','indicator_original','unit_original']].drop_duplicates()


def _read_eclac_best_csv(path):
 """Recover the same top-per-Fiscal-code ECLAC selection from cached best mapping output."""
 x=pd.read_csv(path,dtype=str,low_memory=False).fillna('')
 ren={
  'fiscal_survey_code':'wbfd_mnemonic','Fiscal Survey Code':'wbfd_mnemonic',
  'fiscal_survey_variable':'wbfd_variable','Fiscal Survey Variable':'wbfd_variable',
  'eclac_indicator_id':'INDICATOR_ID','ECLAC Indicator ID':'INDICATOR_ID',
  'eclac_series_label':'indicator_original','ECLAC Series / Classification':'indicator_original',
  'unit':'unit_original','Unit':'unit_original',
 }
 x=x.rename(columns={k:v for k,v in ren.items() if k in x.columns and v not in x.columns})
 for c in ['wbfd_mnemonic','wbfd_variable','INDICATOR_ID','indicator_original','unit_original']:
  if c not in x: x[c]=''
 x['wbfd_mnemonic']=x['wbfd_mnemonic'].astype(str).str.strip().str.upper()
 x['INDICATOR_ID']=x['INDICATOR_ID'].astype(str).str.replace(r'\.0$','',regex=True).str.strip()
 x['indicator_original']=x['indicator_original'].astype(str).str.strip()
 x['unit_original']=x['unit_original'].astype(str).str.strip()
 x=x[(x.wbfd_mnemonic!='')&(x.wbfd_mnemonic!='NAN')&(x.INDICATOR_ID!='')].copy()
 if x.empty: return x
 x['_score']=pd.to_numeric(x.get('mapping_score',''),errors='coerce').fillna(-1e18)
 x['_countries']=pd.to_numeric(x.get('countries',''),errors='coerce').fillna(-1)
 x['_rows']=pd.to_numeric(x.get('rows',''),errors='coerce').fillna(-1)
 x['_priority']=pd.to_numeric(x.get('priority',''),errors='coerce').fillna(999999)
 x=x.sort_values(['wbfd_mnemonic','_score','_countries','_rows','_priority'],ascending=[True,False,False,False,True])
 x=x.drop_duplicates('wbfd_mnemonic',keep='first')
 return x[['wbfd_mnemonic','wbfd_variable','INDICATOR_ID','indicator_original','unit_original']].drop_duplicates()


def _write_eclac_registry(regdir, exact, note, old_path=None):
 exact=exact.copy().fillna('')
 for c in ['wbfd_mnemonic','wbfd_variable','INDICATOR_ID','indicator_original','unit_original']:
  if c not in exact: exact[c]=''
 exact['wbfd_mnemonic']=exact['wbfd_mnemonic'].astype(str).str.strip().str.upper()
 exact['INDICATOR_ID']=exact['INDICATOR_ID'].astype(str).str.replace(r'\.0$','',regex=True).str.strip()
 exact['indicator_original']=exact['indicator_original'].astype(str).str.strip()
 exact['unit_original']=exact['unit_original'].astype(str).str.strip()
 exact=exact[(exact.wbfd_mnemonic!='')&(exact.INDICATOR_ID!='')].copy().drop_duplicates('wbfd_mnemonic',keep='first')
 if exact.empty: raise RuntimeError('Could not reconstruct any exact ECLAC mappings.')
 exact['institution']='ECLAC'; exact['registry_status']='LOCKED'; exact['registry_note']=note
 for c in lock.AUDIT_FIELDS['ECLAC']:
  if c not in exact: exact[c]=''
 exact['signature']=exact.apply(lambda r:lock.signature_row(r,'ECLAC'),axis=1)
 cols=['institution','signature','registry_status','registry_note','wbfd_mnemonic','wbfd_variable']+lock.AUDIT_FIELDS['ECLAC']
 out=exact[list(dict.fromkeys(cols))].drop_duplicates().reset_index(drop=True)
 regdir.mkdir(parents=True,exist_ok=True)
 p=regdir/'ECLAC_LOCKED.csv'
 if old_path is not None and Path(old_path).exists():
  stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
  backup=regdir/f'ECLAC_LOCKED_pre_rebuild_{stamp}.csv'
  shutil.copy2(old_path,backup)
  print(f'  ECLAC old registry backup: {backup}',flush=True)
 out.to_csv(p,index=False,encoding='utf-8-sig')
 try: out.to_excel(regdir/'ECLAC_LOCKED.xlsx',index=False)
 except Exception: pass
 return out


def ensure_eclac_registry_exact(base,regdir):
 """Guarantee a non-empty ECLAC LOCKED registry without thematic-tree discovery."""
 p=regdir/'ECLAC_LOCKED.csv'
 reg=pd.DataFrame()
 if p.exists():
  try: reg=pd.read_csv(p,dtype=str,low_memory=False).fillna('')
  except Exception as e: print(f'  WARNING: could not read existing ECLAC_LOCKED.csv: {e}',flush=True)
 for c in ['INDICATOR_ID','wbfd_mnemonic','indicator_original','unit_original']:
  if c not in reg: reg[c]=''
 if len(reg):
  reg['wbfd_mnemonic']=reg['wbfd_mnemonic'].astype(str).str.strip().str.upper()
  reg['INDICATOR_ID']=reg['INDICATOR_ID'].astype(str).str.replace(r'\.0$','',regex=True).str.strip()
  valid=reg['wbfd_mnemonic'].ne('') & reg['wbfd_mnemonic'].ne('NAN') & reg['INDICATOR_ID'].ne('')
 else:
  valid=pd.Series(dtype=bool)
 if len(reg)>0 and valid.all():
  print(f"ECLAC LOCKED registry: exact source signatures OK ({len(reg)} rows, {reg['INDICATOR_ID'].nunique()} indicators)",flush=True)
  return reg
 locked_codes=set(reg.loc[reg['wbfd_mnemonic'].ne('') & reg['wbfd_mnemonic'].ne('NAN'),'wbfd_mnemonic']) if len(reg) else set()
 print(f"ECLAC LOCKED registry needs recovery: rows={len(reg)}, usable_rows={int(valid.sum()) if len(valid) else 0}, locked_codes={len(locked_codes)}",flush=True)
 for wb in _eclac_mapping_workbook_candidates(base):
  try: exact=_read_eclac_exact_map(wb)
  except Exception as e:
   print(f'  ECLAC recovery skip workbook {wb}: {e}',flush=True); continue
  if locked_codes: exact=exact[exact['wbfd_mnemonic'].isin(locked_codes)].copy()
  if not exact.empty and (not locked_codes or locked_codes.issubset(set(exact['wbfd_mnemonic']))):
   note=('ECLAC exact source signatures reconstructed from prior mapping workbook; no thematic-tree annual discovery performed. '
         'Existing locked Fiscal Survey choices were preserved.' if locked_codes else
         'ECLAC LOCKED registry bootstrapped from prior ECLAC Mapped Only selection; no thematic-tree annual discovery performed.')
   out=_write_eclac_registry(regdir,exact,note,p if p.exists() else None)
   print(f"ECLAC LOCKED registry AUTO-RECOVERED from workbook: {wb}",flush=True)
   print(f"  locked codes={out['wbfd_mnemonic'].nunique()}, exact rows={len(out)}, indicators={out['INDICATOR_ID'].nunique()}",flush=True)
   return out
 for csv in _eclac_mapping_csv_candidates(base):
  try: exact=_read_eclac_best_csv(csv)
  except Exception as e:
   print(f'  ECLAC recovery skip csv {csv}: {e}',flush=True); continue
  if locked_codes: exact=exact[exact['wbfd_mnemonic'].isin(locked_codes)].copy()
  if not exact.empty and (not locked_codes or locked_codes.issubset(set(exact['wbfd_mnemonic']))):
   note='ECLAC exact source signatures reconstructed from cached eclac_to_fiscal_mapping_best.csv; no thematic-tree annual discovery performed.'
   out=_write_eclac_registry(regdir,exact,note,p if p.exists() else None)
   print(f"ECLAC LOCKED registry AUTO-RECOVERED from cached mapping CSV: {csv}",flush=True)
   print(f"  locked codes={out['wbfd_mnemonic'].nunique()}, exact rows={len(out)}, indicators={out['INDICATOR_ID'].nunique()}",flush=True)
   return out
 # Last recovery path: the previous broad ECLAC run may have finished downloading
 # all series and written downloaded_fiscal_series_inventory.csv before annual mapping
 # export was skipped. Re-run only the deterministic mapping algorithm OFFLINE.
 for inv in _eclac_series_inventory_candidates(base):
  try: exact=_read_eclac_from_series_inventory(inv,base)
  except Exception as e:
   print(f'  ECLAC recovery skip cached inventory {inv}: {e}',flush=True); continue
  if locked_codes: exact=exact[exact['wbfd_mnemonic'].isin(locked_codes)].copy()
  if not exact.empty and (not locked_codes or locked_codes.issubset(set(exact['wbfd_mnemonic']))):
   note=('ECLAC LOCKED registry rebuilt OFFLINE from cached downloaded_fiscal_series_inventory.csv '
         'using the same embedded mapping rules; no thematic-tree/API discovery performed.')
   out=_write_eclac_registry(regdir,exact,note,p if p.exists() else None)
   print(f"ECLAC LOCKED registry AUTO-RECOVERED OFFLINE from cached series inventory: {inv}",flush=True)
   print(f"  locked codes={out['wbfd_mnemonic'].nunique()}, exact rows={len(out)}, indicators={out['INDICATOR_ID'].nunique()}",flush=True)
   return out
 searched=([str(x) for x in _eclac_mapping_workbook_candidates(base)] +
           [str(x) for x in _eclac_mapping_csv_candidates(base)] +
           [str(x) for x in _eclac_series_inventory_candidates(base)])
 raise RuntimeError(
  'ECLAC_LOCKED.csv is empty/broken and no prior exact ECLAC mapping artifact could be recovered. '
  'The annual runner intentionally will NOT start thematic-tree discovery. Copy the previous '
  'eclac_data_latest_*/metadata/ECLAC_CEPALSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx into this package '
  '(or mapping_tables/) and rerun. Searched artifacts: '+('; '.join(searched) if searched else 'none found')
 )



def _eurostat_mapping_workbook_candidates(base):
 """Find prior Eurostat mapping workbooks in the current or nearby pipeline packages."""
 out=[]
 def add(p):
  p=Path(p)
  if p.exists() and p.is_file() and p not in out: out.append(p)
 for p in [
  base/'mapping_tables'/'EUROSTAT_to_Fiscal_Survey_Mapping.xlsx',
  base/'EUROSTAT_to_Fiscal_Survey_Mapping.xlsx',
 ]: add(p)
 for root in sorted([p for p in base.glob('eurostat_data_latest_*') if p.is_dir()],key=lambda p:p.stat().st_mtime,reverse=True):
  add(root/'metadata'/'EUROSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx')
 for parent in _adb_search_roots(base)[1:]:
  try:
   siblings=[]
   for pat in ['fiscal_pipeline*','fiscal_mapping*']:
    siblings.extend([d for d in parent.glob(pat) if d.is_dir()])
   siblings=sorted(set(siblings),key=lambda d:d.stat().st_mtime,reverse=True)[:30]
   for d in siblings:
    for pat in [
     '**/EUROSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx',
     '**/EUROSTAT_to_Fiscal_Survey_Mapping.xlsx',
    ]:
     for q in sorted(d.glob(pat),key=lambda x:x.stat().st_mtime,reverse=True)[:5]: add(q)
  except Exception:
   pass
 return out


def _eurostat_mapping_csv_candidates(base):
 """Find cached Eurostat best-mapping CSVs without re-running bulk discovery."""
 out=[]
 def add(p):
  p=Path(p)
  if p.exists() and p.is_file() and p not in out: out.append(p)
 for root0 in _adb_search_roots(base):
  candidates=[]
  try:
   if root0==base:
    candidates += list(base.glob('eurostat_data_latest_*/metadata/eurostat_to_fiscal_mapping_best.csv'))
   else:
    for pat in ['fiscal_pipeline*','fiscal_mapping*']:
     for d in [x for x in root0.glob(pat) if x.is_dir()][:30]:
      candidates += list(d.glob('**/eurostat_to_fiscal_mapping_best.csv'))
  except Exception:
   pass
  for q in sorted(set(candidates),key=lambda x:x.stat().st_mtime,reverse=True)[:20]: add(q)
 return out


def _eurostat_series_inventory_candidates(base):
 """Find cached Eurostat series inventories for offline mapping recovery."""
 out=[]
 def add(p):
  p=Path(p)
  if p.exists() and p.is_file() and p not in out: out.append(p)
 for root0 in _adb_search_roots(base):
  candidates=[]
  try:
   if root0==base:
    candidates += list(base.glob('eurostat_data_latest_*/metadata/eurostat_fiscal_series_inventory.csv'))
   else:
    for pat in ['fiscal_pipeline*','fiscal_mapping*']:
     for d in [x for x in root0.glob(pat) if x.is_dir()][:30]:
      candidates += list(d.glob('**/eurostat_fiscal_series_inventory.csv'))
  except Exception:
   pass
  for q in sorted(set(candidates),key=lambda x:x.stat().st_mtime,reverse=True)[:20]: add(q)
 return out


def _eurostat_key_value(key, names):
 import re
 for name in names:
  m=re.search(rf'(?:^|\\|){re.escape(name)}=([^|\\[]+)',str(key or ''),re.I)
  if m: return m.group(1).strip()
 return ''


def _normalize_eurostat_exact(x):
 x=x.copy().fillna('')
 ren={
  'Fiscal Survey Code':'wbfd_mnemonic','fiscal_survey_code':'wbfd_mnemonic','WBFD Mnemonic':'wbfd_mnemonic',
  'Fiscal Survey Variable':'wbfd_variable','fiscal_survey_variable':'wbfd_variable',
  'Eurostat Dataset':'source_dataset','dataset':'source_dataset','SOURCE_DATASET':'source_dataset',
  'Eurostat Indicator Key':'indicator_original','eurostat_indicator_key':'indicator_original','Source Indicator':'indicator_original',
  'Unit':'unit_original','unit':'unit_original',
 }
 x=x.rename(columns={k:v for k,v in ren.items() if k in x.columns and v not in x.columns})
 for c in ['wbfd_mnemonic','wbfd_variable','source_dataset','indicator_original','unit_original','TRANSACTION','UNIT_MEASURE']:
  if c not in x: x[c]=''
 for c in ['Final Runtime Status','Final Mapping Status']:
  if c in x.columns:
   v=x[c].astype(str).str.strip().str.upper()
   x=x[~v.isin(['','GAP','REJECTED','DROP','NO'])].copy()
 x['wbfd_mnemonic']=x['wbfd_mnemonic'].astype(str).str.strip().str.upper()
 x['source_dataset']=x['source_dataset'].astype(str).str.strip().str.lower()
 x['indicator_original']=x['indicator_original'].astype(str).str.strip()
 x['unit_original']=x['unit_original'].astype(str).str.strip()
 empty_tx=x['TRANSACTION'].astype(str).str.strip().eq('')
 x.loc[empty_tx,'TRANSACTION']=x.loc[empty_tx,'indicator_original'].map(lambda z:_eurostat_key_value(z,['na_item','transaction','indic']))
 empty_unit=x['UNIT_MEASURE'].astype(str).str.strip().eq('')
 x.loc[empty_unit,'UNIT_MEASURE']=x.loc[empty_unit,'indicator_original'].map(lambda z:_eurostat_key_value(z,['unit']))
 x=x[(x.wbfd_mnemonic!='')&(x.wbfd_mnemonic!='NAN')&(x.source_dataset!='')&(x.indicator_original!='')].copy()
 if x.empty: return x
 score_col=next((c for c in ['Runtime Score','Mapping Score','mapping_score'] if c in x.columns),None)
 if score_col:
  x['_score']=pd.to_numeric(x[score_col],errors='coerce').fillna(-1e18)
  x=x.sort_values(['wbfd_mnemonic','_score'],ascending=[True,False]).drop(columns=['_score'])
 x=x.drop_duplicates('wbfd_mnemonic',keep='first')
 return x[['wbfd_mnemonic','wbfd_variable','source_dataset','indicator_original','unit_original','TRANSACTION','UNIT_MEASURE']].drop_duplicates()


def _read_eurostat_exact_map(path):
 xl=pd.ExcelFile(path)
 pref=['Eurostat Mapped Only','LOCKED Mapping','Mapped Only','Eurostat Full Mapping']
 sh=next((x for x in pref if x in xl.sheet_names),None)
 if sh is None: sh=next((x for x in xl.sheet_names if 'mapped only' in x.lower()),None)
 if sh is None: return pd.DataFrame()
 return _normalize_eurostat_exact(pd.read_excel(path,sheet_name=sh,dtype=str).fillna(''))


def _read_eurostat_best_csv(path):
 return _normalize_eurostat_exact(pd.read_csv(path,dtype=str,low_memory=False).fillna(''))


def _read_eurostat_from_series_inventory(path,base):
 inv=pd.read_csv(path,low_memory=False).fillna('')
 required={'dataset','indicator_key'}
 if inv.empty or not required.issubset(inv.columns): return pd.DataFrame()
 helper=base/'eurostat_main.py'
 if not helper.exists(): return pd.DataFrame()
 spec=importlib.util.spec_from_file_location('_eurostat_mapping_recovery_helper',helper)
 mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
 _,best=mod.build_mapping(inv)
 if best is None or best.empty: return pd.DataFrame()
 return _normalize_eurostat_exact(best)


def _write_eurostat_registry(regdir,exact,note,old_path=None):
 exact=_normalize_eurostat_exact(exact)
 if exact.empty: raise RuntimeError('Could not reconstruct any exact EUROSTAT mappings.')
 exact['institution']='EUROSTAT'; exact['registry_status']='LOCKED'; exact['registry_note']=note
 for c in lock.AUDIT_FIELDS['EUROSTAT']:
  if c not in exact: exact[c]=''
 exact['signature']=exact.apply(lambda r:lock.signature_row(r,'EUROSTAT'),axis=1)
 cols=['institution','signature','registry_status','registry_note','wbfd_mnemonic','wbfd_variable']+lock.AUDIT_FIELDS['EUROSTAT']
 out=exact[list(dict.fromkeys(cols))].drop_duplicates().reset_index(drop=True)
 regdir.mkdir(parents=True,exist_ok=True); p=regdir/'EUROSTAT_LOCKED.csv'
 if old_path is not None and Path(old_path).exists():
  stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); backup=regdir/f'EUROSTAT_LOCKED_pre_rebuild_{stamp}.csv'
  shutil.copy2(old_path,backup); print(f'  EUROSTAT old registry backup: {backup}',flush=True)
 out.to_csv(p,index=False,encoding='utf-8-sig')
 try: out.to_excel(regdir/'EUROSTAT_LOCKED.xlsx',index=False)
 except Exception: pass
 return out


def ensure_eurostat_registry_exact(base,regdir):
 """Guarantee exact Eurostat dataset+indicator signatures without bulk annual discovery."""
 p=regdir/'EUROSTAT_LOCKED.csv'; reg=pd.DataFrame()
 if p.exists():
  try: reg=pd.read_csv(p,dtype=str,low_memory=False).fillna('')
  except Exception as e: print(f'  WARNING: could not read existing EUROSTAT_LOCKED.csv: {e}',flush=True)
 for c in ['source_dataset','indicator_original','wbfd_mnemonic']:
  if c not in reg: reg[c]=''
 if len(reg):
  reg['wbfd_mnemonic']=reg['wbfd_mnemonic'].astype(str).str.strip().str.upper()
  reg['source_dataset']=reg['source_dataset'].astype(str).str.strip().str.lower()
  valid=reg['wbfd_mnemonic'].ne('') & reg['wbfd_mnemonic'].ne('NAN') & reg['source_dataset'].ne('') & reg['indicator_original'].astype(str).str.strip().ne('')
 else: valid=pd.Series(dtype=bool)
 if len(reg)>0 and valid.all():
  print(f"EUROSTAT LOCKED registry: exact source signatures OK ({len(reg)} rows, {reg['source_dataset'].nunique()} datasets)",flush=True)
  return reg
 locked_codes=set(reg.loc[reg['wbfd_mnemonic'].ne('') & reg['wbfd_mnemonic'].ne('NAN'),'wbfd_mnemonic']) if len(reg) else set()
 print(f"EUROSTAT LOCKED registry needs recovery: rows={len(reg)}, usable_rows={int(valid.sum()) if len(valid) else 0}, locked_codes={len(locked_codes)}",flush=True)
 for wb in _eurostat_mapping_workbook_candidates(base):
  try: exact=_read_eurostat_exact_map(wb)
  except Exception as e:
   print(f'  EUROSTAT recovery skip workbook {wb}: {e}',flush=True); continue
  if locked_codes: exact=exact[exact['wbfd_mnemonic'].isin(locked_codes)].copy()
  if not exact.empty and (not locked_codes or locked_codes.issubset(set(exact['wbfd_mnemonic']))):
   out=_write_eurostat_registry(regdir,exact,'EUROSTAT exact signatures recovered from prior Eurostat Mapped Only workbook; no annual bulk discovery performed.',p if p.exists() else None)
   print(f"EUROSTAT LOCKED registry AUTO-RECOVERED from workbook: {wb}",flush=True)
   print(f"  locked codes={out['wbfd_mnemonic'].nunique()}, exact rows={len(out)}, datasets={out['source_dataset'].nunique()}",flush=True)
   return out
 for csv in _eurostat_mapping_csv_candidates(base):
  try: exact=_read_eurostat_best_csv(csv)
  except Exception as e:
   print(f'  EUROSTAT recovery skip csv {csv}: {e}',flush=True); continue
  if locked_codes: exact=exact[exact['wbfd_mnemonic'].isin(locked_codes)].copy()
  if not exact.empty and (not locked_codes or locked_codes.issubset(set(exact['wbfd_mnemonic']))):
   out=_write_eurostat_registry(regdir,exact,'EUROSTAT exact signatures recovered from cached eurostat_to_fiscal_mapping_best.csv; no annual bulk discovery performed.',p if p.exists() else None)
   print(f"EUROSTAT LOCKED registry AUTO-RECOVERED from cached mapping CSV: {csv}",flush=True)
   print(f"  locked codes={out['wbfd_mnemonic'].nunique()}, exact rows={len(out)}, datasets={out['source_dataset'].nunique()}",flush=True)
   return out
 for inv in _eurostat_series_inventory_candidates(base):
  try: exact=_read_eurostat_from_series_inventory(inv,base)
  except Exception as e:
   print(f'  EUROSTAT recovery skip cached inventory {inv}: {e}',flush=True); continue
  if locked_codes: exact=exact[exact['wbfd_mnemonic'].isin(locked_codes)].copy()
  if not exact.empty and (not locked_codes or locked_codes.issubset(set(exact['wbfd_mnemonic']))):
   out=_write_eurostat_registry(regdir,exact,'EUROSTAT LOCKED registry rebuilt OFFLINE from cached eurostat_fiscal_series_inventory.csv using the same mapping rules; no API discovery performed.',p if p.exists() else None)
   print(f"EUROSTAT LOCKED registry AUTO-RECOVERED OFFLINE from cached series inventory: {inv}",flush=True)
   print(f"  locked codes={out['wbfd_mnemonic'].nunique()}, exact rows={len(out)}, datasets={out['source_dataset'].nunique()}",flush=True)
   return out
 searched=[str(x) for x in (_eurostat_mapping_workbook_candidates(base)+_eurostat_mapping_csv_candidates(base)+_eurostat_series_inventory_candidates(base))]
 raise RuntimeError('EUROSTAT_LOCKED.csv is empty/broken and no prior Eurostat mapping artifact could be recovered. Annual runner will NOT start bulk discovery. Searched artifacts: '+('; '.join(searched) if searched else 'none found'))

def build_plan(regdir,selected,path):
 plan={}
 for s in selected:
  r=lock.load_registry(s,regdir).fillna(''); d={'rows':r.to_dict('records'),'fiscal_codes':sorted(set(r.get('wbfd_mnemonic',pd.Series(dtype=str)).astype(str))- {''})}
  if s=='ADB':
   d['flows']=sorted(set(r.get('SOURCE_DATAFLOW',pd.Series(dtype=str)).astype(str).str.strip())- {''})
   d['indicator_codes']=sorted(set(r.get('ADB_INDICATOR_CODE',pd.Series(dtype=str)).astype(str).str.strip())- {''})
   if not d['flows'] or not d['indicator_codes']:
    raise RuntimeError('ADB LOCKED registry has no exact flow/indicator signatures after repair.')
   print(f"ADB refresh plan: LOCKED rows={len(r)}, flows={len(d['flows'])}, indicators={len(d['indicator_codes'])}; broad discovery DISABLED",flush=True)
  elif s=='ECLAC':
   d['indicator_ids']=sorted(set(r.get('INDICATOR_ID',pd.Series(dtype=str)).astype(str).str.strip())- {''})
   if r.empty or not d['indicator_ids']:
    raise RuntimeError('ECLAC LOCKED registry has no usable rows/indicator IDs after recovery.')
   source_series=r[['INDICATOR_ID','indicator_original','unit_original']].drop_duplicates().shape[0] if all(c in r.columns for c in ['INDICATOR_ID','indicator_original','unit_original']) else len(r)
   print(f"ECLAC refresh plan: LOCKED rows={len(r)}, indicators={len(d['indicator_ids'])}, source_series={source_series}; thematic-tree discovery DISABLED",flush=True)
  elif s=='EUROSTAT':
   d['datasets']=sorted(set(r.get('source_dataset',pd.Series(dtype=str)).astype(str).str.strip())- {''})
   exact_rows=r[(r.get('source_dataset',pd.Series('',index=r.index)).astype(str).str.strip()!='') & (r.get('indicator_original',pd.Series('',index=r.index)).astype(str).str.strip()!='')] if not r.empty else r
   if r.empty or exact_rows.empty or not d['datasets']:
    raise RuntimeError('EUROSTAT LOCKED registry has no exact dataset+indicator signatures after recovery.')
   print(f"EUROSTAT refresh plan: LOCKED rows={len(exact_rows)}, datasets={len(d['datasets'])}; bulk discovery DISABLED",flush=True)
  elif s=='OECD': d['dataset_families']=sorted(set(r.get('source_dataset',pd.Series(dtype=str)).astype(str))- {''})
  elif s=='AFDB': d['dataset_ids']=sorted(set(r.get('dataset_id',pd.Series(dtype=str)).astype(str))- {''})
  plan[s]=d
 path.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8'); return plan
def run_collector(s,script,base,plan):
 env=os.environ.copy(); env['WBFD_ANNUAL_LOCKED_MODE']='1'; env['WBFD_SKIP_RUNTIME_MAPPING']='1'; env['WBFD_SELECTIVE_REFRESH']='1'; env['WBFD_LOCKED_PLAN']=str(plan); env['WBFD_EFFECTIVE_REGISTRY_DIR']=str(base/'mapping_registry')
 print('\n'+'='*76+f'\nANNUAL DIRECT REFRESH: {s}\n'+'='*76,flush=True)
 subprocess.run([sys.executable,'-u',str(script)],cwd=base,check=True,env=env)
def canonical(x,s):
 x=x.copy()
 aliases={'Fiscal Survey Code':'wbfd_mnemonic','Fiscal Survey Variable':'wbfd_variable'}
 x=x.rename(columns={k:v for k,v in aliases.items() if k in x.columns and v not in x.columns})
 for c in ['wbfd_mnemonic','wbfd_variable','country_original','country_code','year','value_original','unit_original','indicator_original']:
  if c not in x: x[c]=''
 x['wbfd_mnemonic']=x['wbfd_mnemonic'].astype(str).str.strip().str.upper(); x['institution']=s
 x['fiscal_survey_code']=x['wbfd_mnemonic']; x['fiscal_survey_variable']=x['wbfd_variable']; x['country']=x['country_original']; x['value']=pd.to_numeric(x['value_original'],errors='coerce'); x['unit']=x['unit_original']
 return x
def filter_idb(prod,reg):
 if prod.empty:return prod
 p=canonical(prod,'IDB'); r=reg.copy().fillna(''); out=[]
 for _,rr in r.iterrows():
  m=p['wbfd_mnemonic'].eq(str(rr.get('wbfd_mnemonic','')).strip().upper())
  ind=str(rr.get('indicator_original','')).strip()
  if ind and 'indicator_original' in p: m &= p['indicator_original'].astype(str).str.strip().str.lower().eq(ind.lower())
  unit=str(rr.get('unit_original','')).strip()
  if unit and 'unit_original' in p: m &= p['unit_original'].astype(str).str.strip().str.lower().eq(unit.lower())
  if m.any(): out.append(p.loc[m].copy())
 return pd.concat(out,ignore_index=True,sort=False).drop_duplicates() if out else p.iloc[0:0].copy()

def run_idb_rule_mapping_builder(base):
 """Refresh IDB mapping table and mapped observations from the fixed Latin Macro Watch Annual Series.

 This is the user-selected fast rule-based IDB workflow. It downloads the Annual
 Series once, rebuilds the auditable mapping table, and writes mapped 2007-latest
 observations. The annual pipeline then uses exactly those rule-selected mappings.
 """
 builder=base/'idb_lmw_mapping_builder_final.py'
 if not builder.exists(): raise FileNotFoundError(builder)
 mapping_dir=base/'mapping_tables'
 audit_dir=base/'mapping_audit'/'IDB'
 print('\n'+'='*76+'\nIDB RULE-BASED MAPPING + DATA REFRESH\n'+'='*76,flush=True)
 subprocess.run([
  sys.executable,'-u',str(builder),
  '--mapping-dir',str(mapping_dir),
  '--audit-dir',str(audit_dir),
  '--with-data'
 ],cwd=base,check=True)
 return audit_dir


def idb_rule_selected_production(base):
 """Turn the IDB rule-selected best mappings into annual production rows.

 REVIEW vs PRODUCTION from the mapping builder is retained as audit metadata, but
 the user's selected workflow treats the best rule-selected mapping as the current
 IDB mapping table. No legacy IDB_LOCKED filtering is applied here.
 """
 mapped_path=base/'mapping_audit'/'IDB'/'mapped_data'/'IDB_MAPPED_2007_LATEST.csv'
 map_path=base/'mapping_tables'/'IDB_MAPPED_ONLY.csv'
 if not mapped_path.exists(): raise FileNotFoundError(f'IDB rule-selected mapped data missing: {mapped_path}')
 if not map_path.exists(): raise FileNotFoundError(f'IDB mapped-only table missing: {map_path}')
 mapped=pd.read_csv(mapped_path,low_memory=False)
 mapping=pd.read_csv(map_path,dtype=str,low_memory=False).fillna('')
 prod=canonical(mapped,'IDB')
 if 'Final Mapping Status' in prod.columns:
  prod['IDB Rule Mapping Status']=prod['Final Mapping Status'].astype(str)
 prod['Final Mapping Status']='RULE_SELECTED'
 prod['mapping_source']='IDB_LMW_RULE_BUILDER'
 codes=set(mapping.get('Fiscal Survey Code',pd.Series(dtype=str)).astype(str).str.strip().str.upper())-{'','NAN'}
 if codes:
  prod=prod[prod['wbfd_mnemonic'].isin(codes)].copy()
 if prod.empty:
  raise RuntimeError('IDB rule builder produced zero mapped 2007-latest observations.')
 # Write a current mapping snapshot for audit; this is not a manually approved LOCKED registry.
 snap_cols=[c for c in [
  'Fiscal Survey Code','Fiscal Survey Variable','Final Mapping Status','series_id',
  'IDB Indicator','IDB Unit','Government Scope','Basis','Mapping Method',
  'Mapping Confidence','Mapping Score','Matched Phrases'
 ] if c in mapping.columns]
 current=base/'mapping_registry'/'IDB_RULE_SELECTED_CURRENT.csv'
 mapping[snap_cols].to_csv(current,index=False,encoding='utf-8-sig')
 print(f"  IDB rule-selected production: rows={len(prod):,}; codes={prod['wbfd_mnemonic'].nunique():,}",flush=True)
 print(f"  IDB current mapping snapshot: {current}",flush=True)
 return prod, codes

def afdb_direct(root,reg):
 f=root/'data'/'afdb_fiscal_raw_all_datasets.csv'; raw=pd.read_csv(f,dtype=str,low_memory=False).fillna(''); r=reg.fillna('').copy()
 for d in (raw,r):
  d['dataset_id']=d['dataset_id'].astype(str).str.replace(r'\.0$','',regex=True).str.strip(); d['indicator_code']=d['indicator_code'].astype(str).str.replace(r'\.0$','',regex=True).str.strip()
 keep=['dataset_id','indicator_code','wbfd_mnemonic','wbfd_variable']; keep=[c for c in keep if c in r]
 m=raw.merge(r[keep].drop_duplicates(),on=['dataset_id','indicator_code'],how='inner',validate='many_to_many')
 m['source']='AFDB_AIH'; m['country_code']=m.get('country_source_code',''); m['indicator_original']=m.get('indicator_name',''); m['value_original']=pd.to_numeric(m['OBS_VALUE'],errors='coerce'); m['unit_original']=m.get('unit_original',''); m['year']=pd.to_numeric(m['year'],errors='coerce').astype('Int64')
 return canonical(m,'AFDB')
def _read_prior_production(path):
 try:
  q=pd.read_csv(path,low_memory=False)
 except Exception:
  return None
 if q is None or q.empty:
  return None
 if 'wbfd_mnemonic' not in q.columns:
  return None
 if q['wbfd_mnemonic'].astype(str).str.strip().eq('').all():
  return None
 return q


def _pipeline_search_roots(base):
 roots=[]
 for q in [base.parent, base.parent.parent]:
  try: q=Path(q).resolve()
  except Exception: continue
  if q not in roots: roots.append(q)
 return roots


def _sibling_pipeline_dirs(base):
 """Bounded search of nearby extracted fiscal-pipeline folders."""
 out=[]
 for parent in _pipeline_search_roots(base):
  try:
   candidates=[]
   for pat in ['fiscal_pipeline*','fiscal_mapping*']:
    candidates += [d for d in parent.glob(pat) if d.is_dir()]
   # Include one nested package directory (common ZIP layout).
   expanded=[]
   for d in candidates:
    expanded.append(d)
    try:
     expanded += [x for x in d.iterdir() if x.is_dir() and (x.name.startswith('fiscal_pipeline') or x.name.startswith('fiscal_mapping'))]
    except Exception:
     pass
   for d in sorted(set(expanded),key=lambda x:x.stat().st_mtime,reverse=True)[:50]:
    try:
     if d.resolve()!=base.resolve() and d not in out: out.append(d)
    except Exception:
     pass
  except Exception:
   pass
 return out


def ensure_locked_registries(base, regdir, selected):
 """Import missing LOCKED registries from a nearby older pipeline package."""
 regdir.mkdir(parents=True,exist_ok=True)
 siblings=_sibling_pipeline_dirs(base)
 for src in selected:
  dst=regdir/f'{src}_LOCKED.csv'
  if dst.exists():
   try:
    z=pd.read_csv(dst,dtype=str,low_memory=False)
    if not z.empty: continue
   except Exception:
    pass
  found=None
  for d in siblings:
   q=d/'mapping_registry'/f'{src}_LOCKED.csv'
   if q.exists():
    try:
     z=pd.read_csv(q,dtype=str,low_memory=False)
     if not z.empty:
      found=q; break
    except Exception:
     continue
  if found is not None:
   shutil.copy2(found,dst)
   xlsx=found.with_suffix('.xlsx')
   if xlsx.exists():
    try: shutil.copy2(xlsx,regdir/xlsx.name)
    except Exception: pass
   print(f'[{src}] imported LOCKED registry from sibling package: {found}',flush=True)




def _load_lock_builder_module(base):
 """Load 02_lock_mapping_registry.py so annual reconciliation uses exactly the same aliases/rules as manual locking."""
 helper=base/'02_lock_mapping_registry.py'
 if not helper.exists():
  raise FileNotFoundError(helper)
 spec=importlib.util.spec_from_file_location('_wbfd_lock_builder',helper)
 mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
 return mod


def _mapping_workbook_candidates_generic(base, source):
 """Find the best reviewed/runtime mapping workbook for reconciliation; never performs network discovery."""
 source=source.upper(); out=[]
 def add(q):
  q=Path(q)
  if q.exists() and q.is_file() and q not in out: out.append(q)

 # Current package is authoritative when present.
 for q in [
  base/'mapping_tables'/f'{source}_to_Fiscal_Survey_Mapping.xlsx',
  base/f'{source}_to_Fiscal_Survey_Mapping.xlsx',
 ]: add(q)
 if source=='OECD': add(base/'OECD_to_Fiscal_Survey_Mapping_VALIDATED_REFERENCE.xlsx')

 runtime_patterns={
  'OECD':['**/OECD_to_Fiscal_Survey_Mapping_RUNTIME.xlsx','**/OECD_to_Fiscal_Survey_Mapping_VALIDATED_REFERENCE.xlsx'],
  'ADB':['**/ADB_KIDB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx','**/ADB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx'],
  'AFDB':['**/AFDB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx','**/AFDB_to_Fiscal_Survey_Mapping.xlsx'],
  'ECLAC':['**/ECLAC_CEPALSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx','**/ECLAC_to_Fiscal_Survey_Mapping_RUNTIME.xlsx'],
  'EUROSTAT':['**/EUROSTAT_to_Fiscal_Survey_Mapping_RUNTIME.xlsx','**/EUROSTAT_to_Fiscal_Survey_Mapping.xlsx'],
  'IDB':['**/IDB_Annual_to_Fiscal_Survey_SURVEY_FIRST.xlsx','**/IDB_to_Fiscal_Survey_Mapping_RUNTIME.xlsx','**/IDB_to_Fiscal_Survey_Mapping.xlsx'],
 }

 # Nearby prior packages are used for reviewed mapping artifacts only.
 candidates=[]
 for d in _sibling_pipeline_dirs(base):
  for pat in [f'**/{source}_to_Fiscal_Survey_Mapping.xlsx'] + runtime_patterns.get(source,[]):
   try: candidates += [q for q in d.glob(pat) if q.is_file()]
   except Exception: pass
 for q in sorted(set(candidates),key=lambda x:x.stat().st_mtime,reverse=True): add(q)
 return out


def _registry_exact_mask(source, r):
 """Rows that contain enough exact source information for the annual direct collector."""
 source=source.upper(); r=r.copy().fillna('')
 if r.empty: return pd.Series(False,index=r.index,dtype=bool)
 def nz(c):
  return r[c].astype(str).str.strip().ne('') if c in r.columns else pd.Series(False,index=r.index)
 code=nz('wbfd_mnemonic')
 if source=='OECD':
  dims=['indicator_original','validated_filter','TRANSACTION','MEASURE','UNIT_MEASURE','EXPENDITURE','INSTR_ASSET','ACCOUNTING_ENTRY','SECTOR','FREQ']
  has_filter=pd.Series(False,index=r.index)
  for c in dims: has_filter |= nz(c)
  return code & nz('source_dataset') & has_filter
 if source=='ADB': return code & nz('SOURCE_DATAFLOW') & nz('ADB_INDICATOR_CODE')
 if source=='AFDB': return code & nz('dataset_id') & nz('indicator_code')
 if source=='ECLAC': return code & nz('INDICATOR_ID')
 if source=='EUROSTAT': return code & nz('source_dataset') & nz('indicator_original')
 if source=='IDB': return code
 return code


def _registry_summary(source, r):
 r=r.copy().fillna('') if r is not None else pd.DataFrame()
 if 'wbfd_mnemonic' not in r: r['wbfd_mnemonic']=''
 codes=set(r['wbfd_mnemonic'].astype(str).str.strip().str.upper())-{'','NAN','NONE'}
 mask=_registry_exact_mask(source,r)
 exact_codes=set(r.loc[mask,'wbfd_mnemonic'].astype(str).str.strip().str.upper())-{'','NAN','NONE'} if len(r) else set()
 return {'rows':len(r),'codes':codes,'exact_rows':int(mask.sum()) if len(mask) else 0,'exact_codes':exact_codes}


def _write_registry_atomic(source, regdir, reg, note):
 """Back up and replace one LOCKED registry only after a complete reviewed mapping table has parsed successfully."""
 source=source.upper(); reg=reg.copy().fillna('')
 p=regdir/f'{source}_LOCKED.csv'; regdir.mkdir(parents=True,exist_ok=True)
 if p.exists():
  stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
  backup=regdir/f'{source}_LOCKED_pre_reconcile_{stamp}.csv'
  shutil.copy2(p,backup)
  print(f'  [{source}] previous LOCKED backup: {backup}',flush=True)
 if 'registry_note' in reg.columns:
  reg['registry_note']=note
 reg.to_csv(p,index=False,encoding='utf-8-sig')
 try: reg.to_excel(regdir/f'{source}_LOCKED.xlsx',index=False)
 except Exception: pass
 return p


def reconcile_mapping_to_locked(base, regdir, selected):
 """
 Reconcile reviewed mapping tables against executable LOCKED registries BEFORE downloads.

 Mapping table is source-of-truth whenever a usable reviewed/runtime mapping workbook exists.
 If mapped Fiscal Survey codes and exact executable LOCKED codes differ, rebuild LOCKED from
 that mapping table. This prevents the historical failure mode: mapping rows visible in Excel
 but annual plan contains zero executable rows.
 """
 locker=_load_lock_builder_module(base)
 rows=[]
 for source in selected:
  map_path=None; map_reg=pd.DataFrame(); map_error=''
  for wb in _mapping_workbook_candidates_generic(base,source):
   try:
    candidate=locker.to_registry(source,locker.sheet(wb,source)).fillna('')
   except Exception as e:
    map_error=str(e); continue
   sm=_registry_summary(source,candidate)
   # A mapping workbook is useful for reconciliation only if it contains executable rows.
   if sm['codes'] and sm['exact_codes']:
    map_path=wb; map_reg=candidate; break

  locked_path=regdir/f'{source}_LOCKED.csv'
  try: locked=pd.read_csv(locked_path,dtype=str,low_memory=False).fillna('') if locked_path.exists() else pd.DataFrame()
  except Exception: locked=pd.DataFrame()
  ls=_registry_summary(source,locked)
  before=dict(ls)
  ms=_registry_summary(source,map_reg)
  action='NO_MAPPING_WORKBOOK_USE_LOCKED'

  if map_path is not None:
   missing_exact=ms['codes']-ls['exact_codes']
   extra_locked=ls['codes']-ms['codes']
   # Exact code sets must match the reviewed mapping table. Row counts themselves may differ
   # if a source intentionally carries more than one exact signature for a code.
   if (not locked_path.exists()) or missing_exact or extra_locked or not ls['exact_codes']:
    note=f'Auto-reconciled from reviewed mapping table {map_path.name}; mapping table is source of truth for annual direct refresh.'
    _write_registry_atomic(source,regdir,map_reg,note)
    locked=map_reg.copy(); ls=_registry_summary(source,locked)
    action='AUTO_REBUILT_FROM_MAPPING_TABLE'
    print(f'[{source}] Mapping→LOCKED AUTO-RECONCILED: mapping codes={len(ms["codes"])}, exact LOCKED codes={len(ls["exact_codes"])} from {map_path}',flush=True)
   else:
    action='MATCH_OK'
    print(f'[{source}] Mapping→LOCKED OK: mapping codes={len(ms["codes"])}, exact LOCKED codes={len(ls["exact_codes"])}',flush=True)
  else:
   print(f'[{source}] No usable mapping workbook found for direct reconciliation; validating existing/recovered LOCKED registry.',flush=True)

  rows.append({
   'institution':source,
   'mapping_workbook':str(map_path or ''),
   'mapping_rows':ms['rows'],
   'mapping_codes':len(ms['codes']),
   'mapping_exact_codes':len(ms['exact_codes']),
   'locked_rows_before':before['rows'],
   'locked_codes_before':len(before['codes']),
   'locked_exact_codes_before':len(before['exact_codes']),
   'locked_rows_after_reconcile':ls['rows'],
   'locked_codes_after_reconcile':len(ls['codes']),
   'locked_exact_codes_after_reconcile':len(ls['exact_codes']),
   'action':action,
   'mapping_parse_note':map_error,
  })

 report=pd.DataFrame(rows)
 regdir.mkdir(parents=True,exist_ok=True)
 report.to_csv(regdir/'MAPPING_LOCKED_RECONCILIATION.csv',index=False,encoding='utf-8-sig')
 return report



def finalize_reconciliation_report(regdir, selected):
 """Add final post-recovery LOCKED counts without erasing the original reconciliation action."""
 p=regdir/'MAPPING_LOCKED_RECONCILIATION.csv'
 try: report=pd.read_csv(p,dtype=str,low_memory=False).fillna('') if p.exists() else pd.DataFrame()
 except Exception: report=pd.DataFrame()
 rows=[]
 for source in selected:
  q=regdir/f'{source}_LOCKED.csv'
  try: r=pd.read_csv(q,dtype=str,low_memory=False).fillna('') if q.exists() else pd.DataFrame()
  except Exception: r=pd.DataFrame()
  sm=_registry_summary(source,r)
  rows.append({'institution':source,'locked_rows_final':sm['rows'],'locked_codes_final':len(sm['codes']),'locked_exact_codes_final':len(sm['exact_codes']),'final_status':'OK' if sm['exact_codes'] else 'FAIL'})
 final=pd.DataFrame(rows)
 if report.empty: out=final
 else: out=report.merge(final,on='institution',how='outer')
 out.to_csv(p,index=False,encoding='utf-8-sig')
 return out


def validate_all_locked_exact(regdir, selected):
 """Validate the bundled immutable LOCKED registries before any network call."""
 rows=[]; bad=[]
 for source in selected:
  p=regdir/f'{source}_LOCKED.csv'
  if not p.exists():
   bad.append(f'{source}: bundled LOCKED registry missing'); continue
  try: r=pd.read_csv(p,dtype=str,low_memory=False).fillna('')
  except Exception as e:
   bad.append(f'{source}: LOCKED unreadable ({e})'); continue
  sm=_registry_summary(source,r)
  expected=BUNDLED_EXPECTED_CODES[source]
  got=len(sm['exact_codes'])
  status='OK' if got==expected else 'FAIL'
  rows.append({'institution':source,'locked_rows':sm['rows'],'locked_codes':len(sm['codes']),'exact_usable_codes':got,'expected_exact_codes':expected,'status':status})
  if got!=expected:
   bad.append(f'{source}: expected {expected} bundled exact codes, got {got}')
 report=pd.DataFrame(rows)
 report.to_csv(regdir/'LOCKED_EXECUTABILITY_CHECK.csv',index=False,encoding='utf-8-sig')
 if bad:
  raise RuntimeError('Bundled LOCKED validation failed BEFORE download: '+'; '.join(bad))
 print('BUNDLED LOCKED VALIDATION: '+', '.join(str(r['institution'])+'='+str(r['exact_usable_codes']) for r in rows),flush=True)
 return report

def main():
 ap=argparse.ArgumentParser()
 ap.add_argument('--base',default='.')
 ap.add_argument('--year',type=int,default=datetime.now().year)
 ap.add_argument('--only',nargs='*',choices=list(SOURCES))
 # Default = full annual refresh. Use --resume only to reuse already completed production files.
 ap.add_argument('--resume',action='store_true',help='Resume an interrupted run by skipping institutions with an existing valid production file.')
 ap.add_argument('--no-merge',action='store_true',help='Skip the final master-data merge step.')
 a=ap.parse_args()
 base=Path(a.base).resolve(); selected=a.only or list(SOURCES); regdir=base/'mapping_registry'

 # FINAL SELF-CONTAINED POLICY: bundled LOCKED registries are immutable source of truth.
 # No sibling-package search, no mapping-table reconciliation, and no annual remapping.
 missing=[src for src in selected if not (regdir/f'{src}_LOCKED.csv').exists()]
 if missing: raise RuntimeError('Bundled LOCKED mapping registry missing for: '+', '.join(missing))
 validate_all_locked_exact(regdir,selected)

 out=base/'annual_output'/str(a.year); inst=out/'institution_data'; audit=out/'audit'; prov=out/'provenance'
 [d.mkdir(parents=True,exist_ok=True) for d in (out,inst,audit,prov)]
 for nm in ['BUNDLED_LOCKED_MANIFEST.csv','LOCKED_EXECUTABILITY_CHECK.csv']:
  q=regdir/nm
  if q.exists(): shutil.copy2(q,audit/nm)
 plan_file=out/'LOCKED_REFRESH_PLAN.json'; build_plan(regdir,selected,plan_file)
 checkpoint=out/'ANNUAL_REFRESH_STATUS.csv'

 # Skip decisions use ONLY production files already present in this package/year.
 # Sibling production files are intentionally ignored. The checkpoint is only supporting evidence.
 previous_status={}
 if checkpoint.exists():
  try:
   prev=pd.read_csv(checkpoint,dtype=str).fillna('')
   if 'institution' in prev.columns:
    previous_status={str(r['institution']).upper():str(r.get('status','')) for _,r in prev.iterrows()}
  except Exception:
   previous_status={}

 status=[]; frames=[]
 if a.resume:
  print('RESUME mode: institutions with an existing valid production file are skipped.',flush=True)
 else:
  print('FULL ANNUAL REFRESH mode: all selected institutions are re-downloaded even if prior production files exist.',flush=True)
 print('RUNNER VERSION: ALL_IN_ONE_RULE_MAPPING_V1_20260923',flush=True)
 print('Mapping policy: OECD/ADB/AFDB/ECLAC/EUROSTAT use bundled reviewed LOCKED mappings. IDB rebuilds its mapping table from the fast Latin Macro Watch Annual Series rule engine on each IDB refresh. AMF is excluded. Final merge runs automatically.',flush=True)

 for s in selected:
  prior=inst/f'{s}_fiscal_survey_country_year_PRODUCTION.csv'
  if a.resume and prior.exists():
   q=_read_prior_production(prior)
   can_skip=(q is not None)
   if can_skip:
    if q is None:
     q=pd.read_csv(prior,low_memory=False) if prior.exists() else pd.DataFrame()
    frames.append(q)
    observed=q['wbfd_mnemonic'].nunique() if (not q.empty and 'wbfd_mnemonic' in q.columns) else 0
    status.append({'institution':s,'status':'REFRESH_SUCCESS','production_rows':len(q),'locked_codes':'','observed_codes':observed,'root':'AUTO_SKIPPED_EXISTING'})
    print(f'[{s}] SKIP: existing annual production file found ({len(q):,} rows): {prior}',flush=True)
    continue

  try:
   if s=='IDB':
    root=run_idb_rule_mapping_builder(base)
    prod, locked_codes=idb_rule_selected_production(base)
    reg=pd.DataFrame({'wbfd_mnemonic':sorted(locked_codes)})
   else:
    run_collector(s,base/SOURCES[s],base,plan_file); root=newest(base,PATTERNS[s])
    if root is None: raise RuntimeError(f'{s}: collector produced no recognizable output root')
    reg=lock.load_registry(s,regdir)
    if s=='AFDB': prod=afdb_direct(root,reg)
    else:
     f=root/DIRECT[s]
     if not f.exists(): raise FileNotFoundError(f'{s}: expected direct annual output missing: {f}')
     prod=pd.read_csv(f,low_memory=False)
     prod=canonical(prod,s)
     prod=prod[prod['wbfd_mnemonic'].isin(set(reg['wbfd_mnemonic'].astype(str).str.upper()))].copy()
    locked_codes=set(reg['wbfd_mnemonic'].astype(str).str.upper())- {''}
   if not reg.empty and prod.empty: raise RuntimeError(f'{s}: zero production rows')
   prod.to_csv(prior,index=False,encoding='utf-8-sig'); frames.append(prod)
   obs=set(prod.get('wbfd_mnemonic',pd.Series(dtype=str)).astype(str).str.upper())- {''}
   pd.DataFrame([{'institution':s,'wbfd_mnemonic':c,'status':'OBSERVED' if c in obs else 'MISSING'} for c in sorted(locked_codes)]).to_csv(audit/f'{s}_locked_code_coverage.csv',index=False,encoding='utf-8-sig')
   status.append({'institution':s,'status':'REFRESH_SUCCESS','production_rows':len(prod),'locked_codes':len(locked_codes),'observed_codes':len(obs),'root':str(root),'error':''})
   pd.DataFrame(status).to_csv(checkpoint,index=False,encoding='utf-8-sig')
  except Exception as e:
   # A transient outage at one institution must not kill the full annual refresh.
   # The failed source can be rerun later with --only SOURCE; successful production
   # files from the other institutions remain reusable and the final merge still runs.
   msg=f'{type(e).__name__}: {e}'
   status.append({'institution':s,'status':'REFRESH_FAILED','production_rows':0,'locked_codes':'','observed_codes':0,'root':'','error':msg})
   pd.DataFrame(status).to_csv(checkpoint,index=False,encoding='utf-8-sig')
   print(f'[{s}] REFRESH_FAILED: {msg}',flush=True)
   print(f'[{s}] Continuing with remaining institutions. Rerun later with: python 03_run_annual_refresh.py --only {s}',flush=True)
   continue

 master=pd.concat(frames,ignore_index=True,sort=False) if frames else pd.DataFrame()
 master.to_csv(out/'FISCAL_SURVEY_ALL_INSTITUTIONS_COUNTRY_YEAR.csv',index=False,encoding='utf-8-sig')
 pd.DataFrame(status).to_csv(checkpoint,index=False,encoding='utf-8-sig')
 snap=prov/'mapping_registry_snapshot'; snap.mkdir(exist_ok=True)
 for s in selected:
  for ext in ['csv','xlsx']:
   p=regdir/f'{s}_LOCKED.{ext}'
   if p.exists(): shutil.copy2(p,snap/p.name)
 print('\nANNUAL DIRECT LOCKED PIPELINE COMPLETED')
 print(out/'FISCAL_SURVEY_ALL_INSTITUTIONS_COUNTRY_YEAR.csv')

 # Final integration is part of the production pipeline. If one or more institutions
 # are temporarily unavailable, merge the successful production files and mark the run PARTIAL.
 if not a.no_merge:
  merge_script=base/'04_merge_final_fiscal_data.py'
  if not merge_script.exists(): raise FileNotFoundError(merge_script)
  failed=[r['institution'] for r in status if str(r.get('status',''))=='REFRESH_FAILED']
  cmd=[sys.executable,'-u',str(merge_script),'--base',str(base),'--year',str(a.year)]
  if (not a.only) and not failed: cmd.append('--require-all')
  label='FINAL MERGE: ALL INSTITUTION PRODUCTION DATA' if not failed else 'FINAL MERGE: AVAILABLE INSTITUTION PRODUCTION DATA (PARTIAL)'
  print('\n'+'='*76+'\n'+label+'\n'+'='*76,flush=True)
  subprocess.run(cmd,cwd=base,check=True)
  final_dir=out/'final_merged'
  if failed:
   (out/'FAILED_SOURCES.txt').write_text('\n'.join(failed)+'\n',encoding='utf-8')
   print('\nPARTIAL PIPELINE COMPLETED')
   print('Temporarily failed sources: '+', '.join(failed))
   print('Rerun failed source(s) later with --only SOURCE; merge will refresh automatically.')
  else:
   print('\nFINAL PIPELINE COMPLETED')
  print(final_dir/'01_FISCAL_SURVEY_MASTER_LONG_ALL_SOURCES.csv')
  print(final_dir/'06_COUNTRY_YEAR_WIDE_SAFE_RESOLVED.csv')

if __name__=='__main__':
 try: main()
 except Exception: traceback.print_exc(); sys.exit(1)
