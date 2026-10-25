from __future__ import annotations
from .model import canonical_url, normalize_company, title_similarity, description_similarity


def same_vacancy(a,b):
    """Conservative same-vacancy match: never merge distinct requisitions."""
    from .identity import same_identity
    return same_identity(a,b)


def cluster_jobs(jobs):
    """Index by employer and stable URLs to avoid quadratic whole-pool scans."""
    import re
    from collections import defaultdict
    clusters=[]
    candidates=defaultdict(set)
    for job in jobs:
        company=normalize_company(job.get("company"))
        keys=set()
        if company: keys.add("company:"+company)
        req=str(job.get("requisition_id") or "").strip()
        if req: keys.add("req:"+req)
        for field in ("url","apply_url","page_source_url"):
            url=canonical_url(job.get(field))
            if url:
                keys.add("url:"+url)
                m=re.search(r"linkedin\.com/jobs/view/(?:[^/?#]*-)?(\d{8,})(?:[/?#]|$)",url)
                if m: keys.add("linkedin:"+m.group(1))
        candidate_indices=set()
        for k in keys: candidate_indices.update(candidates[k])
        matched=None
        for idx in sorted(candidate_indices):
            if same_vacancy(job,clusters[idx][0]):
                matched=idx;break
        if matched is None:
            matched=len(clusters);clusters.append([])
        clusters[matched].append(job)
        for k in keys: candidates[k].add(matched)
    return clusters


def representative(cluster):
    def score(j):
        desc=j.get("page_description") or j.get("description") or ""
        date=1 if j.get("page_published_at") or j.get("source_published_at") else 0
        live=1 if j.get("page_verified_live") is True else 0
        direct=0 if j.get("indexed_fallback") else 1
        return (live,direct,date,len(desc))
    best=dict(max(cluster,key=score))
    best["sources_seen"]=sorted({j.get("source") for j in cluster if j.get("source")})
    best["source_records"]=[{k:j.get(k) for k in ("source","source_job_id","url","source_published_at","source_time_semantics","source_posted_label")} for j in cluster]
    return best
