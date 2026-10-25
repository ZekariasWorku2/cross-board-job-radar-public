from __future__ import annotations

import math
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from .model import stable_key, parse_timestamp, canonical_url, clean

CLOSED_MARKERS = (
    "job is no longer available", "position has been filled", "job has expired",
    "no longer accepting applications", "this job is closed",
    "job posting has expired", "this position is no longer available",
)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _headers(config, json_api=False):
    headers = {
        "User-Agent": config["search"]["user_agent"],
        "Accept-Language": "en-US,en;q=0.9",
    }
    headers["Accept"] = "application/json, text/plain, */*" if json_api else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    return headers


def _sleep(config):
    time.sleep(float(config["search"].get("request_delay_seconds", 0)))


def _request(url, config, *, json_api=False, **kwargs):
    kwargs.setdefault("timeout", int(config["search"].get("request_timeout_seconds", 20)))
    kwargs.setdefault("headers", _headers(config, json_api=json_api))
    attempts = max(1, int(config["search"].get("retry_attempts", 4)))
    last = None
    for attempt in range(attempts):
        response = requests.get(url, **kwargs)
        last = response
        _sleep(config)
        if response.status_code not in RETRYABLE_STATUS:
            response.raise_for_status()
            return response
        if attempt + 1 < attempts:
            retry_after = response.headers.get("Retry-After") if getattr(response, "headers", None) else None
            try:
                delay = float(retry_after) if retry_after else min(30.0, 2.0 ** attempt)
            except Exception:
                delay = min(30.0, 2.0 ** attempt)
            time.sleep(delay)
    last.raise_for_status()
    return last


def _as_iso(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value /= 1000
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except Exception:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()):
        return str(value).strip()
    dt = parse_timestamp(str(value))
    return dt.isoformat() if dt else None


def relative_to_iso(text, now):
    s = clean(text)
    if not s:
        return None
    if "today" in s or "just posted" in s or "just now" in s:
        return now
    base = parse_timestamp(now)
    if "yesterday" in s:
        return (base - timedelta(days=1)).isoformat()
    for pattern, unit in (
        (r"(\d+)\s*(?:minute|min)s?\s+ago", "minutes"),
        (r"(\d+)\s*(?:hour|hr)s?\s+ago", "hours"),
        (r"(\d+)\s*days?\s+ago", "days"),
        (r"(\d+)\s*weeks?\s+ago", "weeks"),
    ):
        match = re.search(pattern, s)
        if match:
            return (base - timedelta(**{unit: int(match.group(1))})).isoformat()
    dt = parse_timestamp(text)
    return dt.isoformat() if dt else None


def _fresh(value, now, config):
    from .freshness import source_might_be_recent
    return bool(value) and source_might_be_recent(
        _as_iso(value), now, int(config["freshness"].get("max_age_hours",24)),
        config.get("timezone","America/New_York"))


def _jsonld_nodes(soup):
    out, stack = [], []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ")
        if not raw.strip():
            continue
        try:
            import json
            stack.append(json.loads(raw))
        except Exception:
            continue
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            item_type = item.get("@type")
            if item_type == "JobPosting" or (isinstance(item_type, list) and "JobPosting" in item_type):
                out.append(item)
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            stack.extend(item)
    return out


def _job_from_jsonld(node, source, page_url, now):
    org = node.get("hiringOrganization") or {}
    loc = node.get("jobLocation") or node.get("applicantLocationRequirements") or ""
    if isinstance(loc, list):
        names = []
        for entry in loc:
            if isinstance(entry, dict):
                addr = entry.get("address") or {}
                if isinstance(addr, dict):
                    names.append(", ".join(str(addr.get(k) or "") for k in ("addressLocality", "addressRegion", "addressCountry") if addr.get(k)))
                else:
                    names.append(str(entry.get("name") or ""))
            elif entry:
                names.append(str(entry))
        loc = "; ".join(x for x in names if x)
    elif isinstance(loc, dict):
        addr = loc.get("address") or {}
        loc = ", ".join(str(addr.get(k) or "") for k in ("addressLocality", "addressRegion", "addressCountry") if addr.get(k)) if isinstance(addr, dict) else str(loc.get("name") or "")

    source_job_id = node.get("identifier")
    if isinstance(source_job_id, dict):
        source_job_id = source_job_id.get("value")
    title = node.get("title") or node.get("name") or ""
    company = org.get("name") if isinstance(org, dict) else str(org or "")
    desc = BeautifulSoup(str(node.get("description") or ""), "html.parser").get_text(" ", strip=True)
    remote = str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE" or "remote" in clean(str(loc) + " " + desc)
    salary = ""
    base_salary = node.get("baseSalary")
    if isinstance(base_salary, dict):
        value = base_salary.get("value") or {}
        if isinstance(value, dict):
            lo, hi = value.get("minValue"), value.get("maxValue")
            salary = f"{lo or ''}-{hi or ''}".strip("-") or str(value.get("value") or "")
    return _make_job(
        source, source_job_id, title, company, loc or ("Remote" if remote else ""),
        node.get("url") or page_url, now, posted=node.get("datePosted"), description=desc,
        employment_type=node.get("employmentType") or "", salary=salary,
        workplace_type="remote" if remote else "", time_semantics="page_date_posted",
    )


def _make_job(source, source_job_id, title, company, location, url, now, *, apply_url=None,
              posted=None, description="", employment_type="", salary="", workplace_type="remote",
              time_semantics="source_date"):
    desc = BeautifulSoup(str(description or ""), "html.parser").get_text(" ", strip=True)
    posted_iso = _as_iso(posted)
    url = canonical_url(url)
    apply_url = canonical_url(apply_url or url)
    job = {
        "source": source,
        "source_job_id": str(source_job_id or ""),
        "title": str(title or "").strip(),
        "company": str(company or "").strip(),
        "location": str(location or "Remote").strip(),
        "workplace_type": workplace_type,
        "employment_type": str(employment_type or "").strip(),
        "salary": str(salary or "").strip(),
        "description": desc,
        "url": url,
        "apply_url": apply_url,
        "source_published_at": posted_iso,
        "source_time_semantics": time_semantics if posted_iso else "unknown",
        "source_posted_label": str(posted or "") if posted else "",
        "retrieved_at": now,
    }
    job["key"] = stable_key(source, job["source_job_id"], url, job["title"], job["company"])
    return job


def _unique(jobs, max_records):
    out = {}
    for job in jobs:
        if job.get("key") and job["key"] not in out:
            out[job["key"]] = job
        if len(out) >= max_records:
            break
    return list(out.values())


def fetch_linkedin_guest(lane, config, now):
    """Enumerate every page LinkedIn exposes for each title-family query."""
    results = []
    max_records = int(config["search"].get("max_records_per_source_per_lane", 50000))
    per_query = int(config["search"].get("max_records_per_query", 10000))
    seconds = int(config["freshness"].get("discovery_lookback_days", 3)) * 86400
    for query in config["lanes"][lane]["queries"]:
        if len(results) >= max_records:
            break
        start = 0
        query_seen = set()
        last_page_signature = None
        while start < per_query and len(results) < max_records:
            url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urllib.parse.urlencode({
                "keywords": query,
                "location": "United States",
                "f_WT": "2",
                "f_TPR": f"r{seconds}",
                "sortBy": "DD",
                "start": start,
            })
            text = _request(url, config).text
            soup = BeautifulSoup(text, "html.parser")
            cards = soup.select("li") or soup.select("div.base-search-card")
            page_jobs = []
            for card in cards:
                anchor = card.select_one("a.base-card__full-link") or card.find("a", href=True)
                title_el = card.select_one("h3") or card.select_one(".base-search-card__title")
                if not anchor or not title_el:
                    continue
                company_el = card.select_one("h4") or card.select_one(".base-search-card__subtitle")
                time_el = card.find("time")
                title = title_el.get_text(" ", strip=True)
                company = company_el.get_text(" ", strip=True) if company_el else ""
                href = canonical_url(anchor.get("href"))
                urn = card.get("data-entity-urn") or ""
                jid = urn.split(":")[-1] if urn else re.search(r"/(\d+)(?:\?|$)", href or "")
                if hasattr(jid, "group"):
                    jid = jid.group(1)
                location_el = card.select_one(".job-search-card__location")
                location = location_el.get_text(" ", strip=True) if location_el else "Remote, United States"
                posted_raw = time_el.get("datetime") if time_el and time_el.get("datetime") else (time_el.get_text(" ", strip=True) if time_el else "")
                posted = _as_iso(posted_raw) or relative_to_iso(posted_raw, now)
                job = _make_job("linkedin", jid, title, company, location, href, now, posted=posted,
                                workplace_type="remote", time_semantics="linkedin_posted_label")
                job["source_posted_label"] = time_el.get_text(" ", strip=True) if time_el else ""
                page_jobs.append(job)
            if not page_jobs:
                break
            signature = tuple(sorted(j.get("key") for j in page_jobs if j.get("key")))
            if signature and signature == last_page_signature:
                raise RuntimeError(f"LinkedIn pagination stalled for {query!r} at offset {start}; scan is incomplete")
            last_page_signature = signature
            new_on_page = 0
            for job in page_jobs:
                if job["key"] not in query_seen:
                    query_seen.add(job["key"])
                    results.append(job)
                    new_on_page += 1
                    if len(results) >= max_records:
                        break
            if new_on_page == 0:
                raise RuntimeError(f"LinkedIn pagination repeated results for {query!r} at offset {start}; scan is incomplete")
            if len(page_jobs) < 25:
                break
            start += 25
    return _unique(results, max_records)


def _not_nan(value):
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    return None if str(value).lower() in {"nan", "nat", "none"} else value


def fetch_jobspy(source, lane, config, now, diagnostics=None):
    """Run the board search and report each stage's counts."""
    from jobspy import scrape_jobs
    jobs, errors = [], []
    limit=int(config["search"].get("max_records_per_source_per_lane",50000))
    per_query=int(config["search"].get("max_records_per_query",10000))
    for query in config["lanes"][lane]["queries"]:
        if len(jobs)>=limit:
            break
        detail={"lane":lane,"query":query,"returned":0,"after_date":0,"after_remote":0,"error":None}
        if diagnostics is not None: diagnostics.append(detail)
        try:
            frame=scrape_jobs(site_name=source,search_term=query,location="Remote",
                is_remote=True,results_wanted=min(per_query,limit-len(jobs)),
                country_indeed="USA",hours_old=int(config["freshness"].get("max_age_hours",24)),
                description_format="plain",verbose=0)
        except Exception as exc:
            detail["error"]=f"{type(exc).__name__}: {exc}"
            errors.append(f"{query}: {detail['error']}")
            continue
        if frame is None or getattr(frame,"empty",True):
            continue
        records=frame.to_dict("records")
        detail["returned"]=len(records)
        for row in records:
            title=_not_nan(row.get("title")) or ""
            if not title:continue
            posted=_not_nan(row.get("date_posted"))
            # JobSpy's Indeed timestamp has day precision, even when pandas
            # represents it as midnight in a datetime/Timestamp object.
            if posted and hasattr(posted,"date"):
                posted=posted.date().isoformat()
            if posted and not _fresh(posted,now,config):continue
            detail["after_date"]+=1
            company=_not_nan(row.get("company")) or ""
            location=_not_nan(row.get("location")) or ""
            description=_not_nan(row.get("description")) or ""
            direct_url=_not_nan(row.get("job_url_direct")) or _not_nan(row.get("job_url"))
            board_url=_not_nan(row.get("job_url")) or direct_url
            remote=bool(_not_nan(row.get("is_remote"))) or "remote" in clean(location+" "+description)
            detail["after_remote"]+=1
            lo,hi=_not_nan(row.get("min_amount")),_not_nan(row.get("max_amount"))
            currency,interval=_not_nan(row.get("currency")),_not_nan(row.get("interval"))
            salary=f"{currency or ''} {lo or ''}-{hi or ''} {interval or ''}".strip() if lo is not None or hi is not None else ""
            jobs.append(_make_job(source,_not_nan(row.get("id")) or "",title,company,
                location or "Unknown",board_url,now,apply_url=direct_url,posted=posted,
                description=description,employment_type=_not_nan(row.get("job_type")) or "",
                salary=salary,workplace_type="remote" if remote else "unknown",
                time_semantics="jobspy_date_posted"))
    unique=_unique(jobs,limit)
    if not unique and errors and diagnostics is None:
        raise RuntimeError("; ".join(errors[:3]))
    return unique


def _builtin_cards(soup, source, page_url, now):
    jobs = []
    for card in soup.select('[data-id="job-card"], article, li'):
        anchor = card.select_one('a[href*="/job/"]') or card.select_one('a[href*="/jobs/"]')
        if not anchor:
            continue
        href = urllib.parse.urljoin(page_url, anchor.get("href"))
        title_el = card.select_one('[data-id="job-title"], h2, h3')
        title = (title_el or anchor).get_text(" ", strip=True)
        if not title or len(title) > 180:
            continue
        company_el = card.select_one('[data-id="company-title"], [class*="company"]')
        company = company_el.get_text(" ", strip=True) if company_el else ""
        text = card.get_text(" ", strip=True)
        posted_match = re.search(r"\b(?:just now|today|yesterday|\d+\s*(?:min|minute|hour|hr|day|week)s?\s+ago)\b", text, re.I)
        posted = relative_to_iso(posted_match.group(0), now) if posted_match else None
        loc_el = card.select_one('[data-id*="location"], [class*="location"]')
        location = loc_el.get_text(" ", strip=True) if loc_el else "Remote"
        jobs.append(_make_job(source, "", title, company, location, href, now, posted=posted,
                              description=text, workplace_type="remote", time_semantics="builtin_card_label"))
    return jobs


def fetch_builtin(lane, config, now):
    jobs = []
    max_records = int(config["search"].get("max_records_per_source_per_lane", 50000))
    for query in config["lanes"][lane]["queries"]:
        page = 1
        previous = None
        while len(jobs) < max_records:
            params = {"search": query}
            if page > 1:
                params["page"] = page
            response = _request("https://builtin.com/jobs/remote?" + urllib.parse.urlencode(params), config)
            soup = BeautifulSoup(response.text, "html.parser")
            found = [_job_from_jsonld(node, "builtin", response.url, now) for node in _jsonld_nodes(soup)]
            found.extend(_builtin_cards(soup, "builtin", response.url, now))
            found = _unique(found, max_records)
            if not found:
                break
            signature = tuple(sorted(j.get("key") for j in found if j.get("key")))
            if signature == previous:
                break
            previous = signature
            jobs.extend(found)
            page += 1
    return _unique(jobs, max_records)


def fetch_remoteok(lane, config, now):
    data = _request("https://remoteok.com/api", config, json_api=True).json()
    jobs = []
    for item in data[1:] if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ("https://remoteok.com/remote-jobs/" + str(item.get("id")))
        jobs.append(_make_job("remoteok", item.get("id"), item.get("position"), item.get("company"),
                              item.get("location") or "Remote", url, now, posted=item.get("date") or item.get("epoch"),
                              description=item.get("description") or "", salary=item.get("salary") or "",
                              workplace_type="remote", time_semantics="remoteok_date"))
    return jobs


def fetch_wwr(lane, config, now):
    response = _request("https://weworkremotely.com/remote-jobs.rss", config)
    root = ET.fromstring(response.content)
    jobs = []
    for item in root.findall(".//item"):
        get = lambda tag: (item.findtext(tag) or "").strip()
        raw_title = get("title")
        company, title = "", raw_title
        if ":" in raw_title:
            company, title = [x.strip() for x in raw_title.split(":", 1)]
        url = get("link")
        jobs.append(_make_job("weworkremotely", get("guid") or url, title, company, "Remote", url, now,
                              posted=get("pubDate"), description=get("description"), workplace_type="remote",
                              time_semantics="rss_published"))
    return jobs


def fetch_himalayas(lane, config, now):
    jobs = []
    max_records = int(config["search"].get("max_records_per_source_per_lane", 50000))
    for query in config["lanes"][lane]["queries"]:
        page = 1
        previous = None
        while len(jobs) < max_records:
            params = {"q": query, "country": "US", "exclude_worldwide": "true", "sort": "recent", "page": page}
            body = _request("https://himalayas.app/jobs/api/search?" + urllib.parse.urlencode(params), config, json_api=True).json()
            rows = body.get("jobs") or body.get("results") or []
            if not rows:
                break
            page_keys = []
            recent_on_page = False
            for item in rows:
                posted = item.get("pubDate")
                if posted and _fresh(posted, now, config):
                    recent_on_page = True
                restrictions = item.get("locationRestrictions") or []
                location = ", ".join(str(x) for x in restrictions) if restrictions else "Remote, United States"
                lo, hi = item.get("minSalary"), item.get("maxSalary")
                salary = f"{item.get('currency') or ''} {lo or ''}-{hi or ''} {item.get('salaryPeriod') or ''}".strip() if lo is not None or hi is not None else ""
                url = item.get("applicationLink") or item.get("url") or item.get("guid")
                job = _make_job("himalayas", item.get("guid"), item.get("title"), item.get("companyName"), location, url, now,
                                posted=posted, description=item.get("description") or item.get("excerpt") or "",
                                employment_type=item.get("employmentType") or "", salary=salary,
                                workplace_type="remote", time_semantics="himalayas_pub_date")
                job["himalayas_guid"]=item.get("guid")
                jobs.append(job); page_keys.append(job["key"])
            signature = tuple(sorted(page_keys))
            if signature == previous:
                raise RuntimeError(f"Himalayas pagination stalled for {query!r} on page {page}")
            previous = signature
            if not recent_on_page:
                break
            page += 1
    return _unique(jobs, max_records)


def fetch_jobicy(lane, config, now):
    # Jobicy's public API exposes up to 200 records but no pagination parameter.
    # Query every title-family variant at the maximum public page size.
    jobs = []
    max_records = int(config["search"].get("max_records_per_source_per_lane", 50000))
    for query in config["lanes"][lane]["queries"]:
        if len(jobs) >= max_records:
            break
        url = "https://jobicy.com/api/v2/remote-jobs?" + urllib.parse.urlencode({"count": 200, "geo": "usa", "tag": query})
        body = _request(url, config, json_api=True).json()
        for item in body.get("jobs") or []:
            job_type = item.get("jobType") or []
            if isinstance(job_type, list):
                job_type = ", ".join(str(x) for x in job_type)
            lo, hi = item.get("salaryMin"), item.get("salaryMax")
            salary = f"{item.get('salaryCurrency') or ''} {lo or ''}-{hi or ''} {item.get('salaryPeriod') or ''}".strip() if lo is not None or hi is not None else ""
            jobs.append(_make_job("jobicy", item.get("id") or item.get("jobSlug"), item.get("jobTitle"), item.get("companyName"),
                                  item.get("jobGeo") or "Remote, United States", item.get("url"), now, posted=item.get("pubDate"),
                                  description=item.get("jobDescription") or item.get("jobExcerpt") or "",
                                  employment_type=job_type, salary=salary, workplace_type="remote", time_semantics="jobicy_pub_date"))
    return _unique(jobs, max_records)


def fetch_themuse(lane, config, now):
    jobs = []
    max_records = int(config["search"].get("max_records_per_source_per_lane", 50000))
    page = 0
    previous = None
    while len(jobs) < max_records:
        url = "https://www.themuse.com/api/public/jobs?" + urllib.parse.urlencode({"page": page, "location": "Flexible / Remote", "descending": "true"})
        body = _request(url, config, json_api=True).json()
        rows = body.get("results") or []
        if not rows:
            break
        page_keys = []
        recent_on_page = False
        for item in rows:
            posted = item.get("publication_date")
            if posted and _fresh(posted, now, config):
                recent_on_page = True
            locations = item.get("locations") or []
            location = "; ".join(str(x.get("name")) for x in locations if isinstance(x, dict) and x.get("name")) or "Flexible / Remote"
            company = (item.get("company") or {}).get("name") if isinstance(item.get("company"), dict) else item.get("company")
            refs = item.get("refs") or {}
            job_url = refs.get("landing_page") if isinstance(refs, dict) else None
            job = _make_job("themuse", item.get("id"), item.get("name"), company, location, job_url, now,
                            posted=posted, description=item.get("contents") or item.get("short_description") or "",
                            employment_type=item.get("type") or "", workplace_type="remote", time_semantics="muse_publication_date")
            jobs.append(job); page_keys.append(job["key"])
        signature = tuple(sorted(page_keys))
        if signature == previous:
            raise RuntimeError(f"The Muse pagination stalled on page {page}")
        previous = signature
        if not recent_on_page:
            break
        page += 1
    return _unique(jobs, max_records)


def fetch_workingnomads(lane, config, now):
    body = _request("https://www.workingnomads.com/api/exposed_jobs/", config, json_api=True).json()
    jobs = []
    for item in body if isinstance(body, list) else []:
        jobs.append(_make_job("workingnomads", item.get("id") or item.get("url"), item.get("title"), item.get("company_name"),
                              item.get("location") or "Remote", item.get("url"), now, posted=item.get("pub_date"),
                              description=item.get("description") or "", employment_type=item.get("category_name") or "",
                              workplace_type="remote", time_semantics="workingnomads_pub_date"))
    return jobs


def fetch_jobopportunities(lane, config, now):
    """Employer-direct public ledger. Public/keyless endpoint is capped at 50 with no cursor."""
    jobs = []
    max_records = int(config["search"].get("max_records_per_source_per_lane", 50000))
    posted_after = (parse_timestamp(now) - timedelta(hours=int(config["freshness"].get("max_age_hours", 24)))).isoformat()
    for query in config["lanes"][lane]["queries"]:
        if len(jobs) >= max_records:
            break
        params = {
            "country": "US",
            "remote": "remote",
            "remote_confirmed": "true",
            "title": query,
            "posted_after": posted_after,
            "include_description": "true",
            "limit": 50,
        }
        body = _request("https://api.jobopportunitiesapi.org/public/jobs?" + urllib.parse.urlencode(params), config, json_api=True).json()
        for item in body.get("data") or []:
            if item.get("remote") not in (None, "remote") and str(item.get("remote")).lower() not in {"true", "remote"}:
                continue
            location = item.get("location") or ", ".join(x for x in (item.get("city"), item.get("state"), item.get("country")) if x) or "Remote, United States"
            salary = item.get("salary_text") or ""
            job = _make_job(
                "jobopportunities", item.get("id") or item.get("slug"), item.get("title"), item.get("company"), location,
                item.get("apply_url") or ("https://www.jobopportunitiesapi.org/jobs/" + str(item.get("slug") or "")), now,
                apply_url=item.get("apply_url"), posted=item.get("posted_at"), description=item.get("description") or "",
                employment_type=item.get("employment_type") or "", salary=salary,
                workplace_type="remote", time_semantics="employer_source_posted_at",
            )
            job["source_public_pagination_limited"] = bool(body.get("has_more") or body.get("paging") == "key_required")
            jobs.append(job)
    return _unique(jobs, max_records)


def fetch_ayla(lane, config, now):
    """US public-sector and contractor board; exhaust page results until the 24h boundary."""
    jobs = []
    max_records = int(config["search"].get("max_records_per_source_per_lane", 50000))
    remote_filter = {"workArrangement": {"remote": "yes"}}
    import json
    for query in config["lanes"][lane]["queries"]:
        page = 0
        previous = None
        while len(jobs) < max_records:
            params = {
                "search": query,
                "page": page,
                "limit": 100,
                "sortBy": "newest",
                "filters": json.dumps(remote_filter, separators=(",", ":")),
            }
            body = _request("https://aylagov.com/api/jobs/search?" + urllib.parse.urlencode(params), config, json_api=True).json()
            rows = body.get("jobs") or body.get("results") or []
            if not rows:
                break
            page_keys = []
            recent_on_page = False
            for item in rows:
                posted = item.get("postedDate")
                if posted and _fresh(posted, now, config):
                    recent_on_page = True
                arrangement = item.get("workArrangement") or ""
                if isinstance(arrangement, dict):
                    remote = str(arrangement.get("remote") or "").lower() in {"yes", "true", "1"}
                else:
                    remote = "remote" in clean(str(arrangement))
                if not remote:
                    continue
                location = item.get("locationText") or ", ".join(x for x in (item.get("city"), item.get("stateCode")) if x) or "Remote, United States"
                salary = item.get("salaryText") or ""
                public_url = "https://aylagov.com/jobs/job/" + str(item.get("id") or "")
                job = _make_job(
                    "ayla", item.get("id"), item.get("title"), item.get("agency"), location, public_url, now,
                    apply_url=item.get("sourceUrl") or public_url, posted=posted,
                    description=" ".join(str(x) for x in (item.get("skills") or [])),
                    employment_type=item.get("jobType") or "", salary=salary,
                    workplace_type="remote", time_semantics="ayla_source_posted_date",
                )
                jobs.append(job); page_keys.append(job["key"])
            signature = tuple(sorted(page_keys))
            if signature and signature == previous:
                raise RuntimeError(f"Ayla pagination stalled for {query!r} on page {page}")
            previous = signature
            total = ((body.get("pagination") or {}).get("total"))
            if not recent_on_page or len(rows) < 100 or (total is not None and (page + 1) * 100 >= int(total)):
                break
            page += 1
    return _unique(jobs, max_records)



def _mcp_prop(schema, names):
    props=(schema or {}).get("properties") or {}
    for name in names:
        if name in props:
            return name
    lowered={str(k).lower():k for k in props}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _mcp_assign(args, schema, names, value):
    name=_mcp_prop(schema,names)
    if name:
        meta=((schema or {}).get("properties") or {}).get(name) or {}
        if "enum" in meta and value not in meta["enum"]:
            lower={str(v).lower():v for v in meta["enum"]}
            if str(value).lower() in lower:
                value=lower[str(value).lower()]
            else:
                return
        args[name]=value


def _mcp_search_args(source, query, schema, config):
    """Build only arguments the remote MCP tool explicitly advertises."""
    args={}
    _mcp_assign(args,schema,("keyword","query","search_term","q"),query)
    _mcp_assign(args,schema,("country","country_code"),"US")

    # Prefer an explicit workplace filter over putting "remote" into free text.
    props=(schema or {}).get("properties") or {}
    workplace=_mcp_prop(schema,("workplace_types","workplace_type","work_arrangement","workplace"))
    if workplace:
        meta=props.get(workplace) or {}
        if meta.get("type")=="array":
            item_enum=((meta.get("items") or {}).get("enum") or [])
            if item_enum:
                choice=next((v for v in item_enum if str(v).lower()=="remote"),None)
                if choice is not None:
                    args[workplace]=[choice]
            else:
                args[workplace]=["Remote"]
        else:
            enum=meta.get("enum") or []
            choice=next((v for v in enum if str(v).lower()=="remote"),None) if enum else "Remote"
            if choice is not None:
                args[workplace]=choice
    else:
        remote_name=_mcp_prop(schema,("remote_only","is_remote","remote"))
        if remote_name:
            meta=props.get(remote_name) or {}
            args[remote_name]=True if meta.get("type") in ("boolean",None) else "remote"
        else:
            _mcp_assign(args,schema,("location",),"Remote")

    # Board recency is discovery evidence only. Employer-origin freshness remains
    # governed by crossboard.freshness.assess().
    posted=_mcp_prop(schema,("posted_date","date_posted"))
    if posted:
        meta=props.get(posted) or {}
        enum=meta.get("enum") or []
        if enum:
            preferred=("ONE","1","ONE_DAY","PAST_24_HOURS","24_HOURS","1d")
            choice=next((v for p in preferred for v in enum if str(v).upper()==p.upper()),None)
            if choice is not None:
                args[posted]=choice
        elif source=="dice":
            args[posted]="ONE"
    for names in (("posted_within_days","days_ago","recency_days","days_since_posted"),
                  ("hours_old","posted_within_hours")):
        key=_mcp_prop(schema,names)
        if key:
            args[key]=1 if "days" in key or "ago" in key else 24
            break

    # Stay within the provider-advertised maximum. One page per title query keeps
    # the 30-minute production cadence respectful and predictable.
    limit_name=_mcp_prop(schema,("jobs_per_page","limit","max_results","page_size","count"))
    if limit_name:
        meta=props.get(limit_name) or {}
        maximum=meta.get("maximum")
        desired=min(int(config["search"].get("max_records_per_query",100)),100)
        if maximum is not None:
            desired=min(desired,int(maximum))
        args[limit_name]=max(1,desired)
    page_name=_mcp_prop(schema,("page_number","page"))
    if page_name:
        args[page_name]=1
    offset_name=_mcp_prop(schema,("offset",))
    if offset_name:
        args[offset_name]=0
    return args


def _mcp_scalar(record, names):
    for name in names:
        value=record.get(name)
        if value not in (None,"",[],{}):
            return value
    return None


def _mcp_job_records(value):
    """Yield probable job dictionaries from structured or text MCP results."""
    if isinstance(value,dict):
        keys={str(k).lower() for k in value}
        title_keys={"title","jobtitle","job_title","name"}
        company_keys={"company","companyname","company_name","employer","employername","employer_name"}
        url_keys={"detailspageurl","applyurl","apply_url","joburl","job_url","url","link"}
        if keys & title_keys and (keys & company_keys or keys & url_keys):
            yield value
        for child in value.values():
            yield from _mcp_job_records(child)
    elif isinstance(value,list):
        for child in value:
            yield from _mcp_job_records(child)


def _mcp_job_from_record(source, record, now):
    title=_mcp_scalar(record,("title","jobTitle","job_title","name"))
    if not title:
        return None
    company=_mcp_scalar(record,("companyName","company_name","company","employerName","employer_name","employer"))
    location=_mcp_scalar(record,("jobLocation","job_location","location","locationName","location_name"))
    if isinstance(location,dict):
        location=", ".join(str(location.get(k) or "") for k in ("city","state","country") if location.get(k)) or str(location.get("name") or "")
    elif isinstance(location,list):
        location=", ".join(str(x.get("name") if isinstance(x,dict) else x) for x in location if x)
    description=_mcp_scalar(record,("description","jobDescription","job_description","summary","snippet")) or ""
    url=_mcp_scalar(record,("detailsPageUrl","details_page_url","applyUrl","apply_url","jobUrl","job_url","url","link"))
    apply_url=_mcp_scalar(record,("applyUrl","apply_url","applicationUrl","application_url")) or url
    posted=_mcp_scalar(record,("postedDate","posted_date","datePosted","date_posted","publicationDate","publication_date","postedAt","posted_at"))
    job_id=_mcp_scalar(record,("id","jobId","job_id","jobID","requisitionId","requisition_id"))
    employment=_mcp_scalar(record,("employmentType","employment_type","jobType","job_type")) or ""
    salary=_mcp_scalar(record,("salary","salaryText","salary_text","compensation")) or ""
    if isinstance(salary,(dict,list)):
        import json
        salary=json.dumps(salary,sort_keys=True)

    workplace=_mcp_scalar(record,("workplaceTypes","workplace_types","workplaceType","workplace_type","workArrangement","work_arrangement"))
    workplace_text=" ".join(map(str,workplace)) if isinstance(workplace,list) else str(workplace or "")
    remote=("remote" in clean(workplace_text+" "+str(location or "")))
    return _make_job(
        source,job_id or "",str(title),str(company or ""),str(location or "Remote"),
        str(url or apply_url or ""),now,apply_url=str(apply_url or url or ""),
        posted=posted,description=str(description),employment_type=str(employment),
        salary=str(salary),workplace_type="remote" if remote else "unknown",
        time_semantics=f"{source}_board_posted_date",
    )


def _mcp_result_values(result):
    values=[]
    structured=getattr(result,"structured_content",None)
    if structured not in (None,{},[]):
        values.append(structured)
    for block in getattr(result,"content",[]) or []:
        text=getattr(block,"text",None)
        if not text:
            continue
        try:
            import json
            values.append(json.loads(text))
        except Exception:
            continue
    return values


def fetch_mcp_jobs(source, lane, config, now):
    """Query an official unauthenticated MCP job-search server."""
    import asyncio
    endpoint=config["sources"][source]["endpoint"]
    limit=int(config["search"].get("max_records_per_source_per_lane",50000))

    async def run():
        from mcp import Client
        jobs=[]; errors=[]
        async with Client(endpoint) as client:
            listing=await client.list_tools()
            tool=next((t for t in listing.tools if t.name=="search_jobs"),None)
            if tool is None:
                raise RuntimeError(f"{source} MCP does not expose search_jobs")
            schema=getattr(tool,"input_schema",None) or getattr(tool,"inputSchema",None) or {}
            for query in config["lanes"][lane]["queries"]:
                if len(jobs)>=limit:
                    break
                try:
                    result=await client.call_tool("search_jobs",_mcp_search_args(source,query,schema,config))
                    if getattr(result,"is_error",False):
                        errors.append(f"{query}: MCP search_jobs returned an error")
                        continue
                    seen_payload=False
                    for value in _mcp_result_values(result):
                        seen_payload=True
                        for record in _mcp_job_records(value):
                            job=_mcp_job_from_record(source,record,now)
                            if job:
                                jobs.append(job)
                                if len(jobs)>=limit:
                                    break
                    if not seen_payload:
                        errors.append(f"{query}: MCP returned no machine-readable payload")
                except Exception as exc:
                    errors.append(f"{query}: {type(exc).__name__}: {exc}")
        if not jobs and errors:
            raise RuntimeError("; ".join(errors[:3]))
        return _unique(jobs,limit)

    return asyncio.run(run())


def fetch_source(source, lane, config, now, diagnostics=None):
    mode = config["sources"][source]["mode"]
    if mode == "linkedin_guest":
        return fetch_linkedin_guest(lane, config, now)
    if mode == "jobspy":
        return fetch_jobspy(source, lane, config, now, diagnostics=diagnostics)
    if mode == "mcp_job_search":
        return fetch_mcp_jobs(source, lane, config, now)
    if mode == "builtin":
        return fetch_builtin(lane, config, now)
    if mode == "remoteok_api":
        return fetch_remoteok(lane, config, now)
    if mode == "weworkremotely_rss":
        return fetch_wwr(lane, config, now)
    if mode == "himalayas_api":
        return fetch_himalayas(lane, config, now)
    if mode == "jobicy_api":
        return fetch_jobicy(lane, config, now)
    if mode == "themuse_api":
        return fetch_themuse(lane, config, now)
    if mode == "workingnomads_api":
        return fetch_workingnomads(lane, config, now)
    if mode == "jobopportunities_api":
        return fetch_jobopportunities(lane, config, now)
    if mode == "ayla_api":
        return fetch_ayla(lane, config, now)
    raise ValueError("unsupported source mode: " + mode)



def _employer_application_link(soup, current_url):
    """Follow an explicitly marked employer application link, not arbitrary links."""
    from .freshness import employer_page
    for anchor in soup.select("a[href]"):
        text=clean(anchor.get_text(" ",strip=True))
        href=urllib.parse.urljoin(current_url,anchor.get("href") or "")
        parsed=urllib.parse.urlsplit(href)
        if parsed.scheme!="https" or not employer_page(href):
            continue
        path=clean(parsed.path)
        if ("apply" in text or "company website" in text or "company site" in text) and (
            "job" in path or "career" in path or "apply" in path or
            any(t in (parsed.hostname or "") for t in ("greenhouse","lever.co","myworkdayjobs","ashbyhq","smartrecruiters","icims"))):
            return href
    return None

def enrich_page(job, config):
    """Verify open status and collect genuine employer/ATS publication evidence."""
    from .freshness import employer_page
    out=dict(job)
    url=job.get("apply_url") or job.get("url")
    if not url:
        out["page_error"]="Missing application URL"
        return out

    def collect_employer_page(response):
        html_text=response.text[:2_000_000]
        soup=BeautifulSoup(html_text,"html.parser")
        out["page_source_url"]=canonical_url(response.url)
        out["page_employer_verified"]=employer_page(response.url)
        text=soup.get_text(" ",strip=True)
        if any(marker in clean(text) for marker in CLOSED_MARKERS):
            out["page_closed_confirmed"]=True
            out["page_verified_live"]=False
        posting=next((n for n in _jsonld_nodes(soup) if isinstance(n,dict) and
                ("JobPosting" in str(n.get("@type") or "") or
                 ("datePosted" in n and "title" in n))),None)
        if posting:
            candidate=_job_from_jsonld(posting,job.get("source") or "page",response.url,
                    job.get("retrieved_at") or datetime.now(timezone.utc).isoformat())
            for k in ("description","location","workplace_type"):
                if candidate.get(k):out["page_"+k]=candidate[k]
            if candidate.get("employment_type"):out["employment_type"]=candidate["employment_type"]
            ident=posting.get("identifier")
            req=ident.get("value") if isinstance(ident,dict) else ident
            if req:out["page_requisition_id"]=str(req)
            if posting.get("datePosted") and employer_page(response.url):
                out["page_published_raw"]=str(posting["datePosted"])
                out["page_published_at"]=_as_iso(posting["datePosted"])
                out["page_time_semantics"]="employer_date_posted"
        elif len(text)>500:
            out["page_description"]=text[:25000]
        host=(urllib.parse.urlsplit(response.url).hostname or "").lower()
        if employer_page(response.url) and ("greenhouse.io" in host or "greenhouse.com" in host):
            m=re.search(r"""["']first_published["']\s*:\s*["']([^"']+)""",html_text,re.I)
            if m:
                out["page_published_raw"]=m.group(1)
                out["page_published_at"]=_as_iso(m.group(1))
                out["page_time_semantics"]="greenhouse_first_published"
        return soup

    try:
        if job.get("source")=="himalayas":
            guid=str(job.get("himalayas_guid") or job.get("source_job_id") or "")
            parts=urllib.parse.urlsplit(guid)
            if parts.hostname not in {"himalayas.app","www.himalayas.app"} or not re.fullmatch(
                    r"/companies/[^/]+/jobs/[^/]+",parts.path.rstrip("/")):
                out["page_error"]="Himalayas API: invalid or missing guid"
                return out
            endpoint="https://himalayas.app/jobs/api"+parts.path.rstrip("/")
            response=_request(endpoint,config,json_api=True,allow_redirects=True)
            detail=response.json().get("job") or {}
            out["page_verified_live"]=True
            out["page_source_url"]=canonical_url(guid)
            out["page_employer_verified"]=False
            if detail.get("description"):
                html_description=str(detail["description"])
                out["page_description"]=BeautifulSoup(html_description,"html.parser").get_text(" ",strip=True)[:25000]
                # Only follow a clearly advertised employer/ATS link; retain
                # the Himalayas application URL for attribution and applying.
                employer_link=_employer_application_link(
                    BeautifulSoup(html_description,"html.parser"),guid)
                if employer_link:
                    employer_response=_request(employer_link,config,allow_redirects=True)
                    if employer_page(employer_response.url):
                        collect_employer_page(employer_response)
            if detail.get("locationRestrictions"):
                places=[str(x.get("name") or x.get("alpha2") or "") if isinstance(x,dict) else str(x)
                        for x in detail["locationRestrictions"]]
                out["page_location"]=", ".join(x for x in places if x)
            # The Himalayas pubDate is last-updated, NOT employer-origin evidence.
            return out
        response=_request(url,config,allow_redirects=True)
        out["page_verified_live"]=True
        soup=collect_employer_page(response)
        if not employer_page(response.url):
            employer_link=_employer_application_link(soup,response.url)
            if employer_link:
                collect_employer_page(_request(employer_link,config,allow_redirects=True))
    except requests.HTTPError as exc:
        code=getattr(exc.response,"status_code",None)
        if job.get("source")=="himalayas" and code==410:
            out["page_verified_live"]=False
            out["page_closed_confirmed"]=True
            out["page_error"]="HTTP 410: Himalayas confirms job closed"
        else:
            out["page_verified_live"]=None
            out["page_error"]=f"HTTP {code or 'unknown'}: {str(exc)[:180]}"
    except Exception as exc:
        out["page_verified_live"]=None
        out["page_error"]=f"{type(exc).__name__}: {str(exc)[:180]}"
    return out
