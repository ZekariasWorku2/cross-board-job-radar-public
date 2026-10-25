"""Durable sent-job identity across source, URL, title, and content variations."""
import re
from urllib.parse import urlsplit
from .model import normalize_company, normalize_title, canonical_url, token_set, jaccard, title_similarity

def _linked_id(value):
    match = re.search(r"linkedin\.com/jobs/view/(?:[^/?#]*-)?(\d{8,})(?:[/?#]|$)", str(value or ""))
    return match.group(1) if match else ""

def _ids(job):
    return {x for x in (str(job.get("requisition_id") or "").strip(),
                        str(job.get("page_requisition_id") or "").strip()) if x}

def _urls(job):
    out=set()
    for field in ("page_source_url", "apply_url", "url"):
        url=canonical_url(job.get(field))
        if url and len(urlsplit(url).path)>2:
            out.add(url)
    return out

def _description(job):
    return job.get("page_description") or job.get("description") or ""

def same_identity(a,b):
    if not a or not b:
        return False
    ac,bc=normalize_company(a.get("company")),normalize_company(b.get("company"))
    ar,br=_ids(a),_ids(b)
    if ar and br and ar.isdisjoint(br):
        return False  # Distinct employer requisitions are distinct vacancies.
    if ac and bc and ac==bc and ar and br and not ar.isdisjoint(br):
        return True
    ai={_linked_id(x) for x in _urls(a)}-{""}
    bi={_linked_id(x) for x in _urls(b)}-{""}
    if ai and bi and ai & bi:
        return True
    if _urls(a) & _urls(b):
        return True
    ad,bd=token_set(_description(a)),token_set(_description(b))
    if ac and bc and ac==bc and title_similarity(a.get("title"),b.get("title"))>=0.70:
        if len(ad)>=5 and ad==bd:
            return True  # Identical description for same employer and title.
        if len(ad)>=20 and len(bd)>=20 and jaccard(ad,bd)>=0.80:
            return True
    return False

def already_sent(state,job,canonical_id=None):
    if canonical_id and any(k.endswith(":"+canonical_id) for k in state.get("sent",{})):
        return True
    # Compare stored full identities; legacy records containing only titles are
    # intentionally not considered proof of identity.
    return any(same_identity(job, record) for record in state.get("sent",{}).values())

def remember_sent(state,lane,job,now):
    cid=job["canonical_id"]
    state.setdefault("sent",{})[f"{lane}:{cid}"]={
        "sent_at":now,"lane":lane,"title":job.get("title"),"company":job.get("company"),
        "requisition_id":job.get("requisition_id") or job.get("page_requisition_id"),
        "page_source_url":job.get("page_source_url"),
        "url":job.get("url"),"apply_url":job.get("apply_url"),
        "description":_description(job)[:12000],
    }
