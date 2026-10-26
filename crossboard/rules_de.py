from __future__ import annotations
import re
from .model import clean
from .remote import remote_status
from .policy import excluded_source, azure_primary
from .resume_match import compare_resume

CORE_GROUPS = {
    "pipelines": ("data pipeline", "data pipelines", "pipeline development", "pipeline architecture"),
    "etl": ("etl", "elt", "extract transform load", "data integration"),
    "warehouse": ("data warehouse", "data warehousing", "dimensional model", "star schema", "snowflake schema"),
    "modeling": ("data model", "data modeling", "data modelling", "schema design"),
    "orchestration": ("airflow", "step functions", "orchestration", "workflow scheduling"),
    "distributed": ("spark", "pyspark", "distributed data", "big data"),
    "lake": ("data lake", "lakehouse"),
}
USER_SKILL_GROUPS = {
    "sql": ("sql", "t-sql", "sql server", "stored procedure", "stored procedures"),
    "etl": ("etl", "elt", "ssis", "data pipeline", "data pipelines"),
    "warehouse": ("data warehouse", "data warehousing", "dimensional model", "star schema"),
    "aws": ("aws", "amazon web services", "s3", "glue", "redshift", "lambda", "step functions"),
    "python_spark": ("python", "pyspark", "spark"),
}
PRIMARY_MISMATCH_TERMS=("gcp","google cloud platform","bigquery","dataflow","azure data factory","microsoft fabric","dbt","databricks","snowflake")
BI_TERMS=("tableau","power bi","dashboard","dashboards","visualization","visualisation","javascript","html","css","business intelligence")
OBTAINABLE=("ability to obtain","able to obtain","eligible to obtain","eligibility to obtain","can obtain","must be eligible","suitability determination")
TITLE_EXCLUDE=("senior","sr","lead","principal","staff","manager","director","architect","intern","internship","apprentice")


def title_matches(title):
    words=set(re.findall(r"[a-z0-9]+", clean(title)))
    return "data" in words and "engineer" in words

def title_rejection(title):
    t=clean(title)
    if not title_matches(title): return "Title does not contain whole words Data and Engineer"
    words=set(re.findall(r"[a-z0-9]+", t))
    for x in TITLE_EXCLUDE:
        if x in words: return "Excluded title: "+x
    if re.search(r"\b(?:qa|quality assurance|security|scientist|analyst)\b",t) or re.search(r"\b(?:ai|ml|machine learning)\b",t):
        return "Title is primarily an adjacent role rather than Data Engineering"
    return None

def _contains_any(text,terms): return any(x in text for x in terms)
def _hits(text,groups): return [n for n,terms in groups.items() if _contains_any(text,terms)]

def _years(text):
    vals=[]
    patterns=(r"\b(?:minimum\s+(?:of\s+)?|at\s+least\s+)?(\d{1,2})\+?\s+(?:years|yrs)(?:\s+of)?\s+(?:relevant\s+|professional\s+|hands[- ]on\s+)?experience\b",r"\b(?:requires?|required|requiring|minimum)\b.{0,55}\b(\d{1,2})\+?\s+(?:years|yrs)\b")
    for p in patterns:
        vals += [int(m.group(1)) for m in re.finditer(p,text)]
    return max(vals) if vals else None

def _travel(text):
    vals=[]
    for p in (r"\b(?:travel|travelling|traveling)\b.{0,35}?\b(\d{1,3})\s*%",r"\b(\d{1,3})\s*%\s+(?:travel|travelling|traveling)\b"):
        vals += [int(m.group(1)) for m in re.finditer(p,text)]
    return max(vals) if vals else None

def clearance_required(text):
    for clause in re.split(r"[.;\n]|\bbut\b|\bhowever\b", text):
        kind=r"(?:secret|top\s+secret|ts\s*/?\s*sci|public\s+trust|security\s+clearance)"
        if not re.search(kind,clause): continue
        if re.search(r"\b(?:no|not)\b.{0,25}\b(?:clearance|required)\b|clearance\s+is\s+not\s+required",clause): continue
        if any(x in clause for x in OBTAINABLE): return True
        if re.search(r"\b(?:active|current)\b.{0,45}"+kind,clause) or re.search(r"must (?:have|hold|possess)|required|with security clearance",clause): return True
    return False

def validate(job,settings=None):
    settings=settings or {}; description=clean(" ".join(str(job.get(k) or "") for k in ("description","page_description"))); text=clean(" ".join(str(job.get(k) or "") for k in ("title","location","page_location","description","page_description")))
    reason=title_rejection(job.get("title") or ""); warnings=[]; review_reasons=[]
    remote,remote_reason=remote_status(job)
    if not reason and excluded_source(job): reason="Excluded repeat-offender source"
    if not reason and azure_primary(job): reason="Azure is the primary technology platform"
    if not reason and remote=="reject": reason=remote_reason
    if remote=="review": review_reasons.append(remote_reason)
    if not reason and job.get("page_closed_confirmed"): reason="Application is confirmed closed"
    elif not reason and job.get("page_verified_live") is not True:
        review_reasons.append("Live application page could not be verified")
    if not reason and clearance_required(text): reason="Requires a security clearance/Public Trust or eligibility/ability to obtain one"
    years=_years(text); max_years=int(settings.get("max_required_years",6))
    if not reason and years is not None and years>max_years: reason=f"Explicit minimum experience requirement is {years} years"
    travel=_travel(text); max_travel=int(settings.get("max_travel_percent",20))
    if not reason and travel is not None and travel>max_travel: reason=f"Requires substantial travel ({travel}%)"
    core=_hits(text,CORE_GROUPS); skills=_hits(text,USER_SKILL_GROUPS); bi=sorted({x for x in BI_TERMS if x in text}); mismatch=sorted({x for x in PRIMARY_MISMATCH_TERMS if x in text})
    if not reason and len(description)>=180 and len(core)<=1 and len(bi)>=3: reason="Description is primarily BI/dashboard work rather than substantive data engineering"
    resume=compare_resume(job)
    base_score=45+min(20,5*len(core))+min(35,7*len(skills))
    score=round(base_score*0.35+resume["resume_fit_score"]*0.65)
    if years is not None and years<=5: score+=4
    elif years==6: score+=1
    if mismatch: score-=min(10,2*len(mismatch))
    if not description: score-=10; warnings.append("Full job-description evidence was not available")
    score=max(0,min(100,score))
    if reason: cls="rejected"
    else:
        strong_evidence=len(core)>=2 and len(skills)>=3 and len(resume["resume_skill_matches"])>=3 and bool(description)
        cls="strong" if score>=int(settings.get("strong_score_threshold",78)) and strong_evidence else "possible"
        if cls=="possible" and (score<int(settings.get("min_possible_alert_score",55)) or len(resume["resume_skill_matches"])<2): reason=f"Insufficient evidence against uploaded resume ({score}/100)"; cls="rejected"
    return {**job,"validation_review_reasons":list(dict.fromkeys(review_reasons)),"validation_class":cls,"validation_score":score,"validation_alertable":cls!="rejected","validation_reject_reason":reason,"validation_warnings":list(dict.fromkeys(warnings)),"validation_required_years":years,"validation_travel_percent":travel,"validation_core_evidence":core,"validation_skill_overlap":skills,"validation_mismatch_terms":mismatch,"validation_jd_evidence":"full_page" if job.get("page_description") else "board_description",**resume}
