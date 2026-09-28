from __future__ import annotations
from pathlib import Path
import hashlib
import pandas as pd

AUDIT_FIELDS={
 "OECD":["source_dataset","indicator_original","validated_filter","TRANSACTION","MEASURE","UNIT_MEASURE","EXPENDITURE","INSTR_ASSET","ACCOUNTING_ENTRY","SECTOR","FREQ"],
 "ADB":["SOURCE_DATAFLOW","ADB_INDICATOR_CODE","indicator_original","unit_original"],
 "AFDB":["dataset_id","indicator_code","indicator_original","unit_original"],
 "AMF":["amf_source_type","amf_source_url","amf_field","amf_item_code","indicator_original"],
 "ECLAC":["INDICATOR_ID","indicator_original","unit_original"],
 "EUROSTAT":["source_dataset","indicator_original","unit_original","TRANSACTION","UNIT_MEASURE"],
 "IDB":["indicator_original","unit_original","SOURCE_RESOURCE","source_resource_id"],
}

def _s(x): return "" if pd.isna(x) else str(x).strip()
def _norm(x): return _s(x).lower()
def _empty(source,note=""):
    cols=["institution","signature","registry_status","registry_note","wbfd_mnemonic","wbfd_variable"]+AUDIT_FIELDS[source.upper()]
    return pd.DataFrame(columns=list(dict.fromkeys(cols)))
def signature_row(row,source):
    source=source.upper(); pieces=[]
    for c in AUDIT_FIELDS[source]:
        v=_s(row.get(c,""))
        if v: pieces.append(f"{c}={v}")
    if not pieces: pieces=[f"wbfd_mnemonic={_s(row.get('wbfd_mnemonic',''))}"]
    return hashlib.sha1((source+"|"+"|".join(pieces)).encode("utf-8")).hexdigest()[:20]
def load_registry(source,regdir="mapping_registry"):
    p=Path(regdir)/f"{source.upper()}_LOCKED.csv"
    if not p.exists(): raise FileNotFoundError(p)
    x=pd.read_csv(p,dtype=str,low_memory=False).fillna("")
    if "wbfd_mnemonic" in x: x["wbfd_mnemonic"]=x["wbfd_mnemonic"].astype(str).str.strip().str.upper()
    return x

def filter_locked(source,data,regdir="mapping_registry"):
    if data is None or data.empty: return pd.DataFrame() if data is None else data.copy()
    reg=load_registry(source,regdir); src=source.upper(); parts=[]
    if reg.empty: return data.iloc[0:0].copy()
    for _,rr in reg.iterrows():
        m=pd.Series(True,index=data.index); used=0
        code=_s(rr.get("wbfd_mnemonic",""))
        if code and "wbfd_mnemonic" in data.columns:
            m &= data["wbfd_mnemonic"].astype(str).str.strip().str.upper().eq(code.upper()); used+=1
        for c in AUDIT_FIELDS[src]:
            want=_s(rr.get(c,""))
            if not want or c not in data.columns: continue
            m &= data[c].astype(str).str.strip().str.lower().eq(want.lower()); used+=1
        if used and m.any(): parts.append(data.loc[m].copy())
    return pd.concat(parts,ignore_index=True,sort=False).drop_duplicates() if parts else data.iloc[0:0].copy()

def audit_against_registry(source,data,regdir="mapping_registry"):
    reg=load_registry(source,regdir); hit=filter_locked(source,data,regdir)
    observed=set(hit.get("wbfd_mnemonic",pd.Series(dtype=str)).astype(str).str.strip().str.upper())
    return reg[~reg.get("wbfd_mnemonic",pd.Series(dtype=str)).astype(str).str.strip().str.upper().isin(observed)].copy()
