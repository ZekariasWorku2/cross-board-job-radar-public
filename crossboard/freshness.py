"""Fail-closed original-publication validation with provenance and date precision.

A board's "3 hours ago" or syndicated RSS timestamp proves only that the job
was visible there. It never proves original employer publication.
"""
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from .model import parse_timestamp

AGGREGATORS = (
    "linkedin.com", "indeed.com", "himalayas.app", "jobicy.com", "remoteok.com",
    "weworkremotely.com", "workingnomads.com", "themuse.com", "ladders.com",
    "ziprecruiter.com", "dice.com", "glassdoor.com", "adzuna.com",
    "torentify.com", "talenthop.com", "wiraa.com",
)
PUBLICATION_SEMANTICS = {"employer_date_posted", "greenhouse_first_published", "employer_first_published"}

def employer_page(url):
    host=(urlsplit(str(url or "")).hostname or "").lower()
    return bool(host) and not any(host==a or host.endswith("."+a) for a in AGGREGATORS)

def _interval(value, semantics, tz_name):
    if value in (None, ""):
        return None
    raw=str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            day=datetime.strptime(raw,"%Y-%m-%d").date()
            start=datetime.combine(day,time.min,tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
            end=datetime.combine(day+timedelta(days=1),time.min,tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
            return (start,end,False)
        except (ValueError, KeyError):
            return None
    dt=parse_timestamp(value)
    if dt:
        return (dt,dt,True)
    return None

def source_might_be_recent(value, now, max_age_hours=24, timezone_name="America/New_York"):
    """A day-only posting date spans the full local date, never midnight UTC."""
    if not value:
        return True
    interval=_interval(value, "source_date", timezone_name)
    observed=parse_timestamp(now)
    if not interval or not observed:
        return True
    start,end,_=interval
    return end>=observed-timedelta(hours=max_age_hours) and start<=observed+timedelta(minutes=10)


def assess(cluster, previous=None, now=None, max_age_hours=24, max_future_skew_minutes=10, timezone_name="America/New_York"):
    now=parse_timestamp(now)
    if not now:
        raise ValueError("Freshness assessment requires a valid observation time")
    previous=previous or {}
    cutoff=now-timedelta(hours=max_age_hours)
    observations=[]
    def add(value,semantics,source,original=False):
        parsed=_interval(value,semantics,timezone_name)
        if parsed:
            start,end,exact=parsed
            observations.append({"start":start,"end":end,"exact":exact,
                 "semantics":semantics,"source":source,"original":original})
    for j in cluster:
        source=j.get("source") or "unknown"
        page=j.get("page_source_url") or ""
        semantics=j.get("page_time_semantics") or "unknown"
        # Only the employer's public page is admissible evidence of original posting.
        if j.get("page_verified_live") is True and employer_page(page) and semantics in PUBLICATION_SEMANTICS:
            add(j.get("page_published_raw") or j.get("page_published_at"),semantics,page,True)
        # Third-party timestamps can establish an older appearance but not fresh origin.
        add(j.get("source_published_at"),j.get("source_time_semantics") or "board_date",source,False)
    previous_date=previous.get("earliest_published_at")
    # Persisted board dates may have day-only precision. Never promote their
    # fabricated midnight UTC value to an exact historical publication time.
    if previous_date and previous.get("freshness_semantics") in PUBLICATION_SEMANTICS:
        add(previous_date,"historical_original_date","state",False)
    if previous.get("first_seen"):
        add(previous["first_seen"],"historical_first_seen","state",False)
    # A historical appearance before the cutoff definitively disqualifies a repost.
    old=[e for e in observations if e["end"]<cutoff or (e["exact"] and e["start"]<cutoff)]
    originals=[e for e in observations if e["original"]]
    confidence="LOW"; decision="INSUFFICIENT_EVIDENCE"; status="unknown"; found=None; reason="No verified employer first-publication evidence"
    if old:
        old.sort(key=lambda o:o["start"])
        found=old[0]
        status="stale";decision="REPOSTED_OLD" if originals and any(o["start"]>=cutoff for o in originals) else "VERIFIED_OLD"
        confidence="HIGH" if found["source"]=="state" or found["original"] else "MEDIUM"
        reason="Same vacancy was publicly observed before the 24-hour cutoff"
    elif originals:
        originals.sort(key=lambda o:o["start"])
        found=originals[0]
        # An interval spanning the cutoff is uncertain; a date-only value must
        # never be converted into a fictional midnight publication timestamp.
        if any(o["start"]<=cutoff<o["end"] for o in originals):
            decision="DATE_BOUNDARY_UNCERTAIN";reason="Date-only evidence overlaps the 24-hour boundary"
        elif any(o["start"]>now+timedelta(minutes=max_future_skew_minutes) for o in originals):
            decision="TIMESTAMP_AMBIGUOUS";reason="Employer publication time is in the future"
        elif any(o["end"]<cutoff for o in originals):
            status="stale";decision="VERIFIED_OLD";confidence="HIGH";reason="Employer original publication precedes cutoff"
        elif all(o["start"]>=cutoff and o["end"]<=now for o in originals):
            status="fresh";decision="VERIFIED_FRESH";confidence="HIGH" if found["semantics"] in {"greenhouse_first_published","employer_first_published"} else "MEDIUM"
            reason="Employer public posting evidence is within 24 hours"
        else:
            decision="DATE_BOUNDARY_UNCERTAIN";reason="Original publication time cannot be bounded within 24 hours"
        if status=="fresh" and len({o["start"] for o in originals})>1:
            # Distinct original-posted timestamps still agree on a fresh result;
            # preserve the earliest and lower exact-time confidence.
            confidence="MEDIUM"
            reason="Employer publication timestamps differ but all establish freshness"
    else:
        if observations:
            found=min(observations,key=lambda o:o["start"])
            decision="INSUFFICIENT_EVIDENCE"
            reason="Only third-party, syndicated, or historically observed timestamps are available"
    age=(now-found["start"]).total_seconds()/3600 if found and found["exact"] else None
    return {"freshness_status":status,
        "earliest_published_at":found["start"].isoformat() if found else None,
        "freshness_confidence":confidence,"freshness_semantics":found["semantics"] if found else "unknown",
        "freshness_source":found["source"] if found else None,
        "freshness_decision":decision,"freshness_reason":reason,
        "freshness_evidence":[{"timestamp":o["start"].isoformat() if o["exact"] else None,
           "date_start":o["start"].isoformat(),"date_end":o["end"].isoformat(),
           "time_verified":o["exact"],"source":o["source"],"semantics":o["semantics"],
           "employer_original":o["original"]} for o in observations],
        "age_hours":round(age,2) if age is not None else None}
