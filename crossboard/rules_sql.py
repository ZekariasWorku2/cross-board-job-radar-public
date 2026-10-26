from __future__ import annotations
import re
from .model import clean, canonical_url, normalize_company, normalize_title, title_similarity
from .remote import remote_status
from .policy import excluded_source, azure_primary, sql_server_primary

GROUPS={
 "sql_server_tsql":(25,("sql server","t-sql","tsql","mssql","microsoft sql")),
 "database_development":(15,("stored procedures","stored procedure","views","database development","database design","relational modeling","data modeling","window functions","functions","triggers")),
 "ssis_etl":(15,("ssis","etl","elt","incremental loads","data integration")),
 "performance":(15,("indexing","indexes","performance tuning","query tuning","execution plans","query optimization")),
 "warehousing":(10,("data warehouse","data warehousing","dimensional modeling","fact tables","dimension tables","data marts","star schema")),
 "quality":(10,("data quality","data validation","reconciliation","referential integrity","exception logging")),
 "production":(10,("sql server agent","production support","troubleshooting","batch processing","run history")),
 "reporting":(10,("ssrs","ssas","reporting services","report builder","paginated reports","power bi report server")),
}

def contains(text,term): return bool(re.search(r"(?<!\w)"+re.escape(term)+r"(?!\w)",text))
def title_lane(title):
    text=clean(title); words=set(re.findall(r"[a-z0-9]+",text)); developer="developer" in words or "developers" in words; engineer="engineer" in words or "engineering" in words; sql=bool({"sql","tsql","mssql"}&words)
    if developer and sql: return "sql_developer"
    if developer and "database" in words: return "database_developer"
    if developer and "ssis" in words: return "ssis_developer"
    if developer and "etl" in words: return "etl_developer"
    if developer and {"data","warehouse"}.issubset(words): return "data_warehouse_developer"
    if engineer and "database" in words: return "database_engineer"
    if engineer and ("etl" in words or {"data","warehouse"} <= words): return "warehouse_or_etl_engineer"
    if developer and ({"ssrs","ssas"} & words): return "sql_reporting_developer"
    if developer and ({"report","reporting"} & words): return "reporting_developer"
    if re.search(r"\b(?:sql server|t sql|tsql|mssql|ssis)\b",text): return "sql_server_family"
    if "sql" in words and ({"engineer","architect","analyst","specialist","developer","development"} & words): return "sql_server_family"
    return None

def title_rejection(title):
    text=clean(title)
    if not title_lane(title): return "Title is outside the SQL Server development discovery lane"
    for word in ("dba","administrator","administration"):
        if contains(text,word): return "Excluded title: "+word
    return None

def clearance_required(text):
    for clause in re.split(r"[.;\n]|\bbut\b|\bhowever\b",text):
        kind=r"(?:secret|top\s+secret|ts\s*/?\s*sci|public\s+trust|security\s+clearance)"
        if not re.search(kind,clause): continue
        if re.search(r"\b(?:no|not)\b.{0,25}\b(?:clearance|required)\b|clearance\s+is\s+not\s+required",clause): continue
        if re.search(r"\b(?:active|current)\b.{0,45}"+kind,clause): return True
        if re.search(r"ability to obtain|able to obtain|eligible to obtain|eligibility|can obtain|must be eligible",clause): return True
        if re.search(r"must (?:have|hold|possess)|required|with security clearance",clause): return True
    return False

def _travel(text):
    vals=[]
    for p in (r"\btravel\b.{0,35}?\b(\d{1,3})\s*%",r"\b(\d{1,3})\s*%\s+travel\b"): vals += [int(m.group(1)) for m in re.finditer(p,text)]
    return max(vals) if vals else None

def validate(job,settings=None):
    settings=settings or {}; title=job.get("title") or ""; lane=title_lane(title); description=clean(" ".join(str(job.get(k) or "") for k in ("description","page_description"))); text=clean(" ".join(str(job.get(k) or "") for k in ("title","location","page_location","description","page_description")))
    reason=title_rejection(title); warnings=[]; review_reasons=[]; remote,rr=remote_status(job)
    if not reason and excluded_source(job):reason="Excluded repeat-offender source"
    if not reason and azure_primary(job):reason="Azure is the primary technology platform"
    if not reason and not sql_server_primary(job):reason="SQL Server is not the primary development technology"
    if not reason and remote=="reject":reason=rr
    if remote=="review":review_reasons.append(rr)
    if not reason and job.get("page_closed_confirmed"):reason="Application is confirmed closed"
    elif not reason and job.get("page_verified_live") is not True:
        review_reasons.append("Live application page could not be verified")
    if not reason and clearance_required(text):reason="Requires a security clearance/Public Trust or eligibility/ability to obtain one"
    if not reason and re.search(r"\.net|dotnet|full[- ]?stack|fullstack",clean(title)) and re.search(r"\bc#\b|asp\.net|\.net core|web api|entity framework|javascript|react|angular",description):reason="Role is primarily .NET/full-stack development rather than SQL database development"
    travel=_travel(text)
    if not reason and travel is not None and travel>int(settings.get("max_travel_percent",20)):reason=f"Requires substantial travel ({travel}%)"
    hits=[name for name,(_,terms) in GROUPS.items() if any(contains(description,t) for t in terms)]; score=sum(GROUPS[k][0] for k in hits); substantive=sql_server_primary(job) and ("sql_server_tsql" in hits or re.search(r"\b(?:sql server|t sql|tsql|mssql|ssis)\b",clean(title)))
    if not reason and lane and not substantive:reason="Job lacks substantive SQL Server development evidence"
    years=[int(n) for n in re.findall(r"(?:minimum (?:of )?|at least |requires?\s*)(\d{1,2})\+?\s*(?:years|yrs)",description)]
    if years and max(years)>int(settings.get("verified_sql_years_review_threshold",12)):warnings.append("High minimum experience requirement; review relevant SQL tenure")
    if len(description)<300:warnings.append("Full job description is incomplete or unavailable")
    if not substantive:warnings.append("Primary SQL Server development focus needs verification")
    if re.search(r"(?:required|must have|expert).{0,45}(?:oracle|pl/sql|informatica|databricks|azure data factory|power bi)",description):warnings.append("Required secondary technology needs a baseline experience check")
    live=job.get("page_verified_live") is True
    if settings.get("require_live_verification",False) and not live and not reason:
        warnings.append("Live application page was not independently verified")
    if substantive: score=max(score,75)
    strong=score>=int(settings.get("strong_score_threshold",80)) and substantive and len(description)>=300 and remote=="ok" and not any("needs a baseline" in w or "High minimum" in w for w in warnings)
    cls="rejected" if reason else "strong" if strong else "possible"; min_possible=int(settings.get("min_possible_alert_score",55)); alertable=cls=="strong" or (cls=="possible" and score>=min_possible)
    if settings.get("require_live_verification",False) and not live:alertable=False
    if cls=="possible" and not alertable:warnings.append(f"Suppressed from email because SQL fit score is below {min_possible}/100 or live verification is missing")
    return {**job,"validation_review_reasons":list(dict.fromkeys(review_reasons)),"validation_title_lane":lane,"validation_class":cls,"validation_score":score,"validation_alertable":alertable,"validation_reject_reason":reason,"validation_warnings":list(dict.fromkeys(warnings)),"validation_core_evidence":hits,"validation_travel_percent":travel,"validation_live_verified":live,"validation_jd_evidence":"full_page" if job.get("page_description") else "board_description"}
