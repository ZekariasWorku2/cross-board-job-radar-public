from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

from . import rules_de, rules_sql
from .policy import excluded_source, azure_primary
from .identity import already_sent as sent_before, remember_sent, same_identity
from .dedupe import cluster_jobs, representative
from .freshness import assess, source_might_be_recent
from .model import parse_timestamp, utc_now_iso, normalize_company, normalize_title, token_set
from .sources import fetch_source, enrich_page

LANE_RULES={"data_engineer":rules_de,"sql_developer":rules_sql}


def load_state(path):
    p=Path(path)
    if not p.exists():
        return {"version":1,"radar":"cross-board-job-radar","sources":{},"jobs":{},"sent":{},"outbox":{},"audit":{},"last_run_at":None}
    import json
    state=json.loads(p.read_text(encoding="utf-8"))
    if state.get("version")!=1 or state.get("radar")!="cross-board-job-radar": raise ValueError("Unsupported or foreign state")
    state.setdefault("audit",{})
    for k in ("sources","jobs","sent","outbox","audit"):
        if not isinstance(state.get(k),dict): raise ValueError("Corrupt state: "+k)
    return state


def canonical_id(cluster):
    reqs=sorted({str(j.get("requisition_id")) for j in cluster if j.get("requisition_id")})
    companies=sorted({normalize_company(j.get("company")) for j in cluster if normalize_company(j.get("company"))})
    titles=sorted({normalize_title(j.get("title")) for j in cluster if normalize_title(j.get("title"))})
    if reqs:
        raw="req|"+"|".join(reqs)+"|"+(companies[0] if companies else "")
    else:
        descriptions=[j.get("page_description") or j.get("description") or "" for j in cluster]
        richest=max(descriptions,key=len,default="")
        signature=" ".join(sorted(token_set(richest))[:120])
        if signature:
            raw="semantic|"+(companies[0] if companies else "")+"|"+(titles[0] if titles else "")+"|"+signature
        else:
            raw="fallback|"+(companies[0] if companies else "")+"|"+(titles[0] if titles else "")
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _eligible_title(job):
    return not excluded_source(job) and not azure_primary(job) and (rules_de.title_matches(job.get("title") or "") or rules_sql.title_lane(job.get("title") or "") is not None)


def _lane_title(job,lane):
    if excluded_source(job) or azure_primary(job): return False
    if lane=="data_engineer": return rules_de.title_matches(job.get("title") or "")
    return rules_sql.title_lane(job.get("title") or "") is not None


def _could_be_fresh(job, now, max_age_hours):
    return source_might_be_recent(job.get("source_published_at"), now, max_age_hours)


def _external_job_id(job):
    """Return the board-visible job/requisition ID when it can be determined."""
    for field in ("source_job_id","requisition_id"):
        value=str(job.get(field) or "").strip()
        if value:
            return value
    source=str(job.get("source") or "").lower()
    url=str(job.get("url") or job.get("apply_url") or "")
    if source=="linkedin":
        match=re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)",url)
        if match:
            return match.group(1)
    return ""


def _job_audit_key(job):
    key=str(job.get("key") or "").strip()
    if key:
        return key
    raw="|".join(str(job.get(k) or "") for k in ("source","url","apply_url","title","company"))
    return hashlib.sha256(raw.encode()).hexdigest()[:28]


def _persistent_audit_key(record):
    source=str(record.get("source") or "unknown")
    external=str(record.get("source_job_id") or "").strip()
    return f"{source}:{external or record.get('job_key') or 'unknown'}"


def _finalize_audit_record(record):
    decisions=record.get("lane_decisions") or []
    statuses=[d.get("delivery_status") for d in decisions]
    if "emailed" in statuses:
        record["final_status"]="emailed"; record["final_reason"]=None
    elif "queued_for_email" in statuses:
        record["final_status"]="queued_for_email"; record["final_reason"]=None
    elif "email_failed" in statuses:
        record["final_status"]="email_failed"; record["final_reason"]="Email delivery failed"
    elif "already_emailed" in statuses:
        record["final_status"]="already_emailed"; record["final_reason"]=None
    elif "suppressed_baseline" in statuses:
        record["final_status"]="suppressed_baseline"; record["final_reason"]="Source was being baselined"
    elif decisions:
        reasons=[]
        for d in decisions:
            reason=d.get("reject_reason")
            if reason and reason not in reasons:
                reasons.append(reason)
        record["final_status"]="rejected"
        record["final_reason"]="; ".join(reasons) or "Did not satisfy lane rules"
    elif record.get("eligible_title") is False:
        record["final_status"]="rejected_pre_enrichment"
        record["final_reason"]="Excluded source, primary Azure role, or title outside the configured job families"
    elif record.get("freshness_candidate") is False:
        record["final_status"]="rejected_pre_enrichment"
        record["final_reason"]="Source timestamp is outside the configured 24-hour discovery window"
    else:
        record["final_status"]="not_evaluated"
        record["final_reason"]="No lane decision was produced"


def _persist_audit(state,rows,now,retention_days):
    audit=state.setdefault("audit",{})
    for record in rows:
        pkey=_persistent_audit_key(record); record["audit_key"]=pkey
        previous=audit.get(pkey,{})
        audit[pkey]={
            "audit_key":pkey,
            "source":record.get("source"),
            "source_job_id":record.get("source_job_id"),
            "job_key":record.get("job_key"),
            "title":record.get("title"),
            "company":record.get("company"),
            "location":record.get("location"),
            "url":record.get("url"),
            "source_published_at":record.get("source_published_at"),
            "first_seen":previous.get("first_seen") or now,
            "last_seen":now,
            "times_seen":int(previous.get("times_seen",0))+1,
            "last_status":record.get("final_status"),
            "last_reason":record.get("final_reason"),
            "last_page_verification":record.get("page_verification"),
            "last_page_error":record.get("page_error"),
            "last_lane_decisions":copy.deepcopy(record.get("lane_decisions") or []),
        }
    cutoff=parse_timestamp(now)-timedelta(days=max(1,int(retention_days)))
    for pkey,record in list(audit.items()):
        last=parse_timestamp(record.get("last_seen"))
        if last and last < cutoff:
            audit.pop(pkey,None)


def scan(config,state,now=None):
    now=now or utc_now_iso(); raw=[]; source_stats={}; source_errors={}
    global_baseline = not bool(state.get("jobs"))
    state.setdefault("audit",{})
    configured=[s for s,c in config["sources"].items() if c.get("enabled")]
    for source in configured:
        total=0; ok=True; details=[]; diagnostics=[]
        for lane in ("data_engineer","sql_developer"):
            if not config["lanes"][lane].get("enabled"): continue
            try:
                if source=="indeed":
                    records=fetch_source(source,lane,config,now,diagnostics=diagnostics)
                else:
                    records=fetch_source(source,lane,config,now)
                for j in records:
                    j["discovered_lane"]=lane
                raw.extend(records); total+=len(records); details.append(f"{lane}:{len(records)}")
            except Exception as exc:
                ok=False; msg=f"{type(exc).__name__}: {exc}"; source_errors[source]=msg; details.append(f"{lane}:error")
        if total == 0:
            ok=False
            if source not in source_errors:
                source_errors[source]="No records returned for the configured 24-hour remote U.S. discovery searches"
        previous=state["sources"].get(source,{})
        initialized=bool(previous.get("initialized_at"))
        if any(d.get("error") for d in diagnostics):
            ok=False
            source_errors.setdefault(source,"Some Indeed queries failed: see query_diagnostics")
        source_stats[source]={"status":"ok" if ok else "degraded","records":total,"details":details,
                              "query_diagnostics":diagnostics,"baseline":not initialized}
        if ok:
            state["sources"][source]={"initialized_at":previous.get("initialized_at") or now,"last_successful_check":now,"last_record_count":total}

    raw_query_hits=len(raw)
    audit_by_key={}
    for job in raw:
        key=_job_audit_key(job); job["key"]=job.get("key") or key
        record=audit_by_key.get(key)
        if record is None:
            record={
                "job_key":key,
                "source":job.get("source"),
                "source_job_id":_external_job_id(job),
                "title":job.get("title"),
                "company":job.get("company"),
                "location":job.get("location"),
                "url":job.get("url") or job.get("apply_url"),
                "source_published_at":job.get("source_published_at"),
                "discovery_hits":0,
                "discovered_lanes":[],
                "eligible_title":None,
                "freshness_candidate":None,
                "page_verification":"not_requested",
                "page_error":None,
                "lane_decisions":[],
                "final_status":"discovered",
                "final_reason":None,
            }
            audit_by_key[key]=record
        record["discovery_hits"]+=1
        lane=job.get("discovered_lane")
        if lane and lane not in record["discovered_lanes"]:
            record["discovered_lanes"].append(lane)

    by_key={}
    for j in raw:
        if j.get("key") and j["key"] not in by_key: by_key[j["key"]]=j
    raw=list(by_key.values())

    max_age=int(config["freshness"].get("max_age_hours",24))
    for job in raw:
        record=audit_by_key[job["key"]]
        record["eligible_title"]=_eligible_title(job)
        if excluded_source(job):
            record["final_reason"]="Posting employer/intermediary is on the permanent exclusion list"
        elif azure_primary(job):
            record["final_reason"]="Azure is the primary technology platform"
        record["freshness_candidate"]=_could_be_fresh(job,now,max_age)
        if record["eligible_title"] and record["freshness_candidate"]:
            record["page_verification"]="pending"
        _finalize_audit_record(record)

    enrich_candidates=[j for j in raw if _eligible_title(j) and _could_be_fresh(j,now,max_age)]
    enrich_candidates.sort(key=lambda j: parse_timestamp(j.get("source_published_at")) or parse_timestamp("1970-01-01"), reverse=True)
    enriched_keys=set(); enriched_map={}
    workers=max(1,int(config["search"].get("page_verification_workers",4)))
    if enrich_candidates:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures={pool.submit(enrich_page,j,config):j for j in enrich_candidates}
            for future in as_completed(futures):
                original=futures[future]
                try:
                    enriched=future.result()
                except Exception:
                    enriched=dict(original); enriched["page_verified_live"]=False; enriched["page_error"]="Unhandled enrichment error"
                enriched_map[original["key"]]=enriched
                enriched_keys.add(original["key"])
    raw=[enriched_map.get(j.get("key"),j) for j in raw]
    for job in raw:
        record=audit_by_key[job["key"]]
        if job["key"] in enriched_keys:
            record["page_verification"]=("failed" if job.get("page_error") and not job.get("page_closed_confirmed")
                else "closed" if job.get("page_closed_confirmed") else
                "verified" if job.get("page_verified_live") else "failed")
            record["page_error"]=job.get("page_error")

    clusters=cluster_jobs([j for j in raw if _eligible_title(j)])
    lane_results={}; all_current_ids=set()
    for lane in ("data_engineer","sql_developer"):
        settings=config["lanes"][lane]; rules=LANE_RULES[lane]; qualifying=[]; rejected=[]; funnel=Counter(); lane_clusters=[]
        for cluster in clusters:
            lane_cluster=[j for j in cluster if _lane_title(j,lane)]
            if not lane_cluster: continue
            lane_clusters.append(lane_cluster); funnel["title_lane"]+=1
            rep=representative(lane_cluster); cid=canonical_id(lane_cluster); all_current_ids.add(cid)
            previous=state["jobs"].get(cid,{})
            if not previous:
                previous=next((v for v in state["jobs"].values() if same_identity(rep,v)),{})
            fresh=assess(lane_cluster,previous,now,int(config["freshness"]["max_age_hours"]),int(config["freshness"]["max_future_skew_minutes"]),config.get("timezone","America/New_York"))
            rep.update(fresh); rep["canonical_id"]=cid
            validated=rules.validate(rep,settings)
            validated.update(fresh); validated["canonical_id"]=cid
            if fresh["freshness_status"]=="fresh": funnel["fresh_24h"]+=1
            else: funnel["stale_or_unknown"]+=1
            review_reasons=list(validated.get("validation_review_reasons") or [])
            if fresh["freshness_decision"] not in {"VERIFIED_FRESH","VERIFIED_OLD","REPOSTED_OLD"}:
                review_reasons.append("Original employer posting date not verified: "+fresh["freshness_decision"])
            quality_ok=(validated.get("validation_class")!="rejected" and
                validated.get("validation_score",0)>=int(settings.get("min_possible_alert_score",55)))
            if fresh["freshness_decision"] in {"VERIFIED_OLD","REPOSTED_OLD"}:
                quality_ok=False
                validated["validation_reject_reason"]="Original posting is confirmed older than 24 hours"
            if quality_ok:
                classification="needs_manual_verification" if review_reasons else "qualified"
            else:
                classification="rejected"
            validated["job_notification_class"]=classification
            validated["verification_needed"]=list(dict.fromkeys(review_reasons)) if quality_ok else []
            validated["validation_alertable"]=bool(quality_ok)
            if classification=="needs_manual_verification":funnel["manual_review"]+=1
            elif classification=="qualified":funnel["fully_qualified"]+=1
            if validated.get("validation_live_verified") or lane=="data_engineer": funnel["live_or_de_not_required"]+=1
            if validated.get("validation_class")=="strong": funnel["strong"]+=1
            elif validated.get("validation_class")=="possible": funnel["possible"]+=1
            else: funnel["rejected"]+=1

            state_record=state["jobs"].get(cid) or previous
            sources=sorted(set((state_record.get("sources_seen") or [])+(validated.get("sources_seen") or [])))
            state["jobs"][cid]={
                "canonical_id":cid,"title":validated.get("title"),"company":validated.get("company"),"first_seen":state_record.get("first_seen") or now,"last_seen":now,
                "earliest_published_at":fresh.get("earliest_published_at") or state_record.get("earliest_published_at"),"freshness_confidence":fresh.get("freshness_confidence") or state_record.get("freshness_confidence"),"freshness_semantics":fresh.get("freshness_semantics") or state_record.get("freshness_semantics"),"sources_seen":sources,
                "requisition_id":validated.get("requisition_id") or validated.get("page_requisition_id"),
                "url":validated.get("url"),"apply_url":validated.get("apply_url"),"page_source_url":validated.get("page_source_url"),
                "description":(validated.get("page_description") or validated.get("description") or "")[:12000],
            }
            baseline_sources=[j.get("source") for j in lane_cluster if source_stats.get(j.get("source"),{}).get("baseline")]
            baseline=global_baseline or bool(baseline_sources)
            already_sent=sent_before(state,validated,cid)
            is_alertable=validated.get("job_notification_class") in {"qualified","needs_manual_verification"}
            if is_alertable and not baseline and not already_sent:
                delivery_status="queued_for_email"; qualifying.append(validated)
            else:
                if is_alertable and baseline:
                    delivery_status="suppressed_baseline"
                elif is_alertable and already_sent:
                    delivery_status="already_emailed"
                else:
                    delivery_status="rejected"
                rejected.append(validated)

            decision={
                "lane":lane,
                "canonical_id":cid,
                "freshness_status":fresh.get("freshness_status"),
                "freshness_decision":fresh.get("freshness_decision"),
                "freshness_reason":fresh.get("freshness_reason"),
                "freshness_confidence":fresh.get("freshness_confidence"),
                "freshness_evidence":copy.deepcopy(fresh.get("freshness_evidence") or []),
                "original_publication_evidence_at":fresh.get("earliest_published_at"),
                "resume_fit_score":validated.get("resume_fit_score"),
                "validation_class":validated.get("validation_class"),
                "validation_score":validated.get("validation_score"),
                "validation_alertable":bool(validated.get("validation_alertable")),
                "notification_class":validated.get("job_notification_class"),
                "verification_needed":validated.get("verification_needed") or [],
                "reject_reason":validated.get("validation_reject_reason"),
                "delivery_status":delivery_status,
            }
            for member in lane_cluster:
                record=audit_by_key.get(member.get("key"))
                if record is not None:
                    record["lane_decisions"].append(copy.deepcopy(decision))
        qualifying.sort(key=lambda j:(j.get("validation_class")=="strong",j.get("validation_score",0)),reverse=True)
        lane_results[lane]={"qualifying":qualifying,
            "manual_review":[j for j in qualifying if j.get("job_notification_class")=="needs_manual_verification"],
            "rejected":rejected,"funnel":dict(funnel),"clusters":len(lane_clusters)}

    for record in audit_by_key.values():
        _finalize_audit_record(record)
    audit_rows=sorted(audit_by_key.values(),key=lambda r:(str(r.get("source") or ""),str(r.get("source_job_id") or ""),str(r.get("job_key") or "")))
    retention_days=int(config.get("audit",{}).get("retention_days",14))
    _persist_audit(state,audit_rows,now,retention_days)

    for lane,result in lane_results.items():
        for job in result["qualifying"]:
            token=f"{lane}:{job['canonical_id']}"
            if any(same_identity(job, e.get("job",{})) for e in state["outbox"].values()):
                continue
            state["outbox"].setdefault(token,{"lane":lane,"canonical_id":job["canonical_id"],"queued_at":now,"job":copy.deepcopy(job)})
    for token,entry in list(state["outbox"].items()):
        sent_key=f"{entry.get('lane')}:{entry.get('canonical_id')}"
        if sent_key in state["sent"] or excluded_source(entry.get("job") or {}) or azure_primary(entry.get("job") or {}):
            state["outbox"].pop(token,None); continue
        posted=parse_timestamp((entry.get("job") or {}).get("earliest_published_at"))
        if posted and parse_timestamp(now)-posted > timedelta(hours=int(config["freshness"]["max_age_hours"])):
            state["outbox"].pop(token,None)

    coverage_limited_sources=sorted({
        j.get("source") for j in raw if j.get("source_public_pagination_limited") and j.get("source")
    })
    health={
        "sources_configured":len(configured),
        "sources_ok":sum(1 for s in configured if source_stats.get(s,{}).get("status")=="ok"),
        "source_errors":source_errors,
        "coverage_limited_sources":coverage_limited_sources,
        "raw_query_hits":raw_query_hits,
        "raw_records":len(raw),
        "unique_jobs":len(clusters),
        "stale_rejected":sum(r["funnel"].get("stale_or_unknown",0) for r in lane_results.values()),
        "page_verifications":len(enriched_keys),
        "page_verification_successes":sum(1 for j in raw if j.get("key") in enriched_keys and j.get("page_verified_live") is True),
        "page_verification_failures":sum(1 for j in raw if j.get("key") in enriched_keys and j.get("page_error") and not j.get("page_closed_confirmed")),
        "qualified_count":sum(v["funnel"].get("fully_qualified",0) for v in lane_results.values()),
        "manual_review_count":sum(v["funnel"].get("manual_review",0) for v in lane_results.values()),
        "page_verification_candidates":len(enrich_candidates),
        "audit_records":len(audit_rows),
    }
    state["last_run_at"]=now
    return {"run_at":now,"health":health,"source_stats":source_stats,"lanes":lane_results,"discovery_audit":audit_rows}


def pending_for_lane(state,lane,max_jobs=10000):
    jobs=[entry["job"] for token,entry in state["outbox"].items() if entry.get("lane")==lane
          and entry.get("job",{}).get("job_notification_class") in {"qualified","needs_manual_verification"}
          and entry.get("job",{}).get("validation_alertable")
          and not excluded_source(entry.get("job") or {}) and not azure_primary(entry.get("job") or {})
          and not sent_before(state,entry.get("job") or {},entry.get("canonical_id"))]
    jobs.sort(key=lambda j:(j.get("job_notification_class")=="qualified",j.get("validation_class")=="strong",j.get("validation_score",0)),reverse=True)
    unique=[]
    for j in jobs:
        if not any(same_identity(j, prior) for prior in unique):
            unique.append(j)
        if len(unique)>=max_jobs: break
    return unique


def mark_sent(state,lane,jobs,now):
    for job in jobs:
        cid=job["canonical_id"]
        remember_sent(state,lane,job,now)
        state["outbox"].pop(f"{lane}:{cid}",None)


def mark_audit_delivery(state,result,lane,jobs,status,error=None):
    cids={j.get("canonical_id") for j in jobs if j.get("canonical_id")}
    if not cids:
        return
    for record in result.get("discovery_audit") or []:
        changed=False
        for decision in record.get("lane_decisions") or []:
            if decision.get("lane")==lane and decision.get("canonical_id") in cids and decision.get("delivery_status")=="queued_for_email":
                decision["delivery_status"]=status
                if error:
                    decision["delivery_error"]=error
                changed=True
        if not changed:
            continue
        _finalize_audit_record(record)
        pkey=record.get("audit_key") or _persistent_audit_key(record)
        persisted=state.setdefault("audit",{}).get(pkey)
        if persisted is not None:
            persisted["last_status"]=record.get("final_status")
            persisted["last_reason"]=record.get("final_reason")
            persisted["last_lane_decisions"]=copy.deepcopy(record.get("lane_decisions") or [])
