from __future__ import annotations

import hashlib
import re
import urllib.parse
from datetime import datetime, timezone
from dateutil import parser as date_parser

TRACKING_KEYS = {"source","src","ref","referral","gh_src","gclid","fbclid","trackingid","trk"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = date_parser.parse(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = text.lower().replace("data bricks", "databricks")
    text = re.sub(r"[^a-z0-9+#./%-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url):
    try:
        p = urllib.parse.urlsplit(str(url or ""))
        if p.scheme not in {"http","https"} or not p.hostname or p.username or p.password:
            return ""
        query=[]
        for k,v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
            lk=k.lower()
            if lk.startswith("utm_") or lk in TRACKING_KEYS:
                continue
            query.append((k,v))
        fragment = p.fragment if "docid=" in p.fragment else ""
        return urllib.parse.urlunsplit(("https",p.netloc.lower(),p.path.rstrip("/") or "/",urllib.parse.urlencode(sorted(query)),fragment))
    except Exception:
        return ""


def stable_key(source, source_job_id=None, url=None, title=None, company=None):
    raw = f"{source}|{source_job_id or ''}|{canonical_url(url)}|{clean(title)}|{clean(company)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:28]


def normalize_company(value):
    text=clean(value)
    text=re.sub(r"\b(incorporated|inc|llc|corp|corporation|company|co|ltd|limited|plc|lp|llp)\b[.,]?", " ", text)
    return re.sub(r"\s+"," ",text).strip()


def normalize_title(value):
    text=clean(value)
    text=re.sub(r"\b(remote|virtual|w2|c2c|usa|us|100|percent|position|role|job|full time|contract)\b", " ", text)
    return re.sub(r"\s+"," ",text).strip()


def token_set(value):
    stop={"the","and","or","a","an","to","of","in","for","with","on","at","by","from","is","are","as","this","that","we","you","our","your","will","be","job","role","position","remote"}
    return {t for t in re.findall(r"[a-z0-9+#]+", clean(value)) if len(t)>2 and t not in stop}


def jaccard(a,b):
    a=set(a); b=set(b)
    if not a or not b:
        return 0.0
    return len(a & b)/len(a | b)


def title_similarity(a,b):
    return jaccard(token_set(normalize_title(a)), token_set(normalize_title(b)))


def description_similarity(a,b):
    return jaccard(token_set(a), token_set(b))
