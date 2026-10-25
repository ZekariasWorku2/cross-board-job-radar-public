"""Strict source exclusions and primary-cloud compatibility."""
import re
from .model import clean, normalize_company

BLOCKED = {"remotehunter", "talenthop", "torentify", "wiraa", "netrolynx ai", "ladders"}

def _name(value):
    return re.sub(r"[^a-z0-9]", "", normalize_company(value))

BLOCKED_KEYS = {_name(x) for x in BLOCKED}

def excluded_source(job):
    """Reject banned posting companies/intermediaries; do not reject LinkedIn itself."""
    return any(_name(job.get(field)) in BLOCKED_KEYS for field in
               ("company", "posting_company", "recruiter", "staffing_company", "hiring_company")
               if job.get(field))

def azure_primary(job):
    """Reject Azure-primary jobs, not AWS/SQL roles with an optional Azure mention."""
    title = clean(job.get("title"))
    desc = clean(" ".join(str(job.get(k) or "") for k in ("page_description", "description")))
    if re.search(r"\bazure\b", title) and not re.search(r"\b(?:aws|amazon web services)\b", title):
        return True
    hard = [
        r"\b(?:required|must have|mandatory|essential|primary|core)\b.{0,85}\b(?:azure data factory|azure synapse|azure databricks|azure data lake|azure cloud|microsoft azure)\b",
        r"\b(?:azure data factory|azure synapse|azure data lake)\b.{0,75}\b(?:required|must have|mandatory|primary|essential)\b",
        r"\b(?:build|develop|manage|architect|maintain)\b.{0,70}\b(?:azure data factory|azure synapse|azure data lake)\b",
        r"\b(?:primarily|exclusively|mainly|focused on|specializ(?:e|ing) in)\b.{0,45}\bazure\b",
    ]
    explicit_primary = bool(re.search(r"\b(?:primary|main|exclusive|exclusively|primarily)\b.{0,45}\b(?:azure|azure data factory|azure synapse)\b|\b(?:azure|azure data factory|azure synapse)\b.{0,45}\b(?:primary|main|exclusive|exclusively)\b", desc))
    azure_stack = sum(bool(re.search(r"\b"+re.escape(x)+r"\b",desc)) for x in ("azure data factory","azure synapse","azure data lake","azure databricks"))
    if not explicit_primary and not (azure_stack>=2 and any(re.search(p,desc) for p in hard)):
        return False
    # Do not reject a clearly AWS-primary role solely for a secondary Azure requirement.
    aws_primary = bool(re.search(r"\b(?:aws|amazon web services)\b.{0,40}\b(?:primary|main|core|focused|platform)\b|\b(?:primary|main|core)\b.{0,35}\b(?:aws|amazon web services)\b", desc))
    return not aws_primary

def sql_server_primary(job):
    title = clean(job.get("title"))
    desc = clean(" ".join(str(job.get(k) or "") for k in ("page_description", "description")))
    explicitly_sql = bool(re.search(r"\b(?:sql server|t sql|tsql|mssql|ssis|ssrs|ssas)\b", title))
    title_sql = bool(re.search(r"\bsql\b", title)) and bool(re.search(r"\b(?:developer|development|engineer|analyst|architect|etl|warehouse|database)\b", title))
    jd_sql = bool(re.search(r"\b(?:sql server|t sql|tsql|mssql|ssis|ssrs|ssas|sql server integration services|sql server reporting services|sql server analysis services)\b", desc))
    jd_hands_on = bool(re.search(r"\b(?:stored procedure|views|index|query tun|etl|ssis|ssrs|ssas|report build|reports|database develop|data warehouse|data model|sql performance|t sql)\b", desc))
    other_primary = bool(re.search(r"\b(?:oracle|postgresql|mysql|bigquery)\b.{0,40}\b(?:primary|main|mandatory|exclusively)\b|\b(?:primary|main)\b.{0,35}\b(?:oracle|postgresql|mysql|bigquery)\b", desc))
    return (explicitly_sql or ((title_sql or jd_sql) and jd_sql and jd_hands_on)) and not other_primary
