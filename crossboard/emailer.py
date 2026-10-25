from __future__ import annotations
import html, os, smtplib
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from .model import parse_timestamp


def _fmt(value,tz="America/New_York"):
    dt=parse_timestamp(value)
    if not dt:return "Unknown"
    return dt.astimezone(ZoneInfo(tz)).strftime("%b %d, %Y %I:%M %p %Z").replace(" 0"," ")

def _subject(lane,jobs,prefix):
    label="Data Engineer" if lane=="data_engineer" else "SQL Developer"
    qualified=sum(j.get("job_notification_class")=="qualified" for j in jobs)
    manual=sum(j.get("job_notification_class")=="needs_manual_verification" for j in jobs)
    return f"{prefix} | {label} | {qualified} Qualified, {manual} Manual Review"

def _smtp_credentials(config):
    username=os.getenv("GMAIL_SMTP_USERNAME","").strip()
    password=os.getenv("GMAIL_SMTP_APP_PASSWORD","").replace(" ","").strip()
    recipient=os.getenv("JOB_ALERT_RECIPIENT","").strip() or config.get("recipient") or username
    return username,password,recipient

def _send_message(msg,config):
    username,password,recipient=_smtp_credentials(config)
    if not username or not password or not recipient:
        return {"sent":False,"configured":False,"error":"Gmail SMTP credentials are not configured"}
    msg["From"]=username; msg["To"]=recipient
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com",465,timeout=30) as smtp:
            smtp.login(username,password); smtp.send_message(msg)
        return {"sent":True,"configured":True,"error":None}
    except Exception as exc:
        return {"sent":False,"configured":True,"error":f"{type(exc).__name__}: {exc}"}

def _status(job):
    return "NEEDS MANUAL VERIFICATION" if job.get("job_notification_class")=="needs_manual_verification" else "QUALIFIED"


def _plain(lane,jobs,health,tz):
    label="Data Engineer" if lane=="data_engineer" else "SQL Developer"
    q=[j for j in jobs if j.get("job_notification_class")=="qualified"]
    m=[j for j in jobs if j.get("job_notification_class")=="needs_manual_verification"]
    lines=[f"Cross-Board Job Radar - {label}","",
           f"Qualified: {len(q)} | Needs Manual Verification: {len(m)}",
           f"Sources successful: {health.get('sources_ok',0)}/{health.get('sources_configured',0)}",""]
    if health.get("source_errors"):
        lines += ["Source issues:"]+[f"- {k}: {v}" for k,v in sorted(health["source_errors"].items())]+[""]
    for section,entries in (("QUALIFIED",q),("NEEDS MANUAL VERIFICATION",m)):
        if not entries: continue
        lines += [section,"="*len(section)]
        for j in entries:
            issues=j.get("verification_needed") or []
            lines += [f"{j.get('title')} - {j.get('company') or 'Unknown company'}",
                f"Status: {_status(j)}",
                f"Location: {j.get('page_location') or j.get('location') or 'Unknown'}",
                f"Source: {', '.join(j.get('sources_seen') or [j.get('source') or 'unknown'])}",
                f"Salary: {j.get('salary') or 'Not provided'} | Fit: {j.get('validation_score')}/100",
                f"Source/evidence date: {_fmt(j.get('earliest_published_at'),tz)}",
                f"Original freshness decision: {j.get('freshness_decision') or 'Unknown'}"]
            if issues: lines+=["Verify manually:"]+["  - "+issue for issue in issues]
            lines += [f"Posting: {j.get('apply_url') or j.get('url') or j.get('page_source_url') or ''}",""]
    return "\n".join(lines)


def _html(lane,jobs,health,tz):
    label="Data Engineer" if lane=="data_engineer" else "SQL Developer"
    source_errors="".join(f"<li><strong>{html.escape(k)}</strong>: {html.escape(str(v))}</li>"
                          for k,v in sorted((health.get("source_errors") or {}).items()))
    chunks=[]
    for classification,heading in (("qualified","Qualified"),("needs_manual_verification","Needs Manual Verification")):
        entries=[j for j in jobs if j.get("job_notification_class")==classification]
        if not entries: continue
        rows=""
        for j in entries:
            link=j.get("apply_url") or j.get("url") or j.get("page_source_url") or ""
            issues="; ".join(j.get("verification_needed") or [])
            warnings="; ".join(j.get("validation_warnings") or [])
            rows += ("<tr>"
                +f"<td>{html.escape(j.get('title') or '')}<br><small>{html.escape(j.get('company') or '')}</small></td>"
                +f"<td>{html.escape(j.get('page_location') or j.get('location') or 'Unknown')}</td>"
                +f"<td>{html.escape(str(j.get('validation_score') or 'Unknown'))}/100</td>"
                +f"<td>{html.escape(str(j.get('freshness_decision') or 'Unknown'))}<br><small>{html.escape(_fmt(j.get('earliest_published_at'),tz))}</small></td>"
                +f"<td>{html.escape(issues or 'All required evidence confirmed')}<br><small>{html.escape(warnings)}</small></td>"
                +f"<td><a href='{html.escape(link,quote=True)}'>Open posting</a></td></tr>")
        chunks.append(f"<h2>{html.escape(heading)} ({len(entries)})</h2>"
            +"<table style='border-collapse:collapse;width:100%' border='1' cellpadding='7'>"
            +"<tr><th>Job</th><th>Location</th><th>Fit</th><th>Original date check</th><th>Items requiring review</th><th>Link</th></tr>"
            +rows+"</table>")
    return (f"<html><body style='font-family:Arial,sans-serif'><h1>Cross-Board Job Radar - {html.escape(label)}</h1>"
        +f"<p>{len(jobs)} new job(s) requiring attention. Manual Review jobs are NOT fully verified.</p>"
        +(("<h3>Source issues</h3><ul>"+source_errors+"</ul>") if source_errors else "")
        +"".join(chunks)+"</body></html>")


def send_lane_email(lane,jobs,health,config):
    tz=config.get("timezone","America/New_York"); prefix=config["notifications"].get("subject_prefix","Cross-Board Job Radar"); msg=EmailMessage(); msg["Subject"]=_subject(lane,jobs,prefix); msg.set_content(_plain(lane,jobs,health,tz)); msg.add_alternative(_html(lane,jobs,health,tz),subtype="html")
    return _send_message(msg,config)

def _health_lines(health,lane_counts=None):
    lane_counts=lane_counts or {}
    lines=[
        f"Sources successful: {health.get('sources_ok',0)}/{health.get('sources_configured',0)}",
        f"Raw query hits: {health.get('raw_query_hits',health.get('raw_records',0))}",
        f"Deduplicated raw records: {health.get('raw_records',0)}",
        f"Unique job clusters: {health.get('unique_jobs',0)}",
        f"Live verification successes: {health.get('page_verification_successes',0)}/{health.get('page_verification_candidates',0)}",
        f"Manual review candidates: {health.get('manual_review_count',0)}",
        f"Data Engineer job alerts (including manual review): {lane_counts.get('data_engineer',0)}",
        f"SQL Developer job alerts (including manual review): {lane_counts.get('sql_developer',0)}",
    ]
    if health.get("source_errors"):
        lines.append("Source issues:")
        lines.extend(f"- {k}: {v}" for k,v in sorted(health["source_errors"].items()))
    else:
        lines.append("Source issues: none")
    return lines

def send_health_email(kind,health,config,run_at=None,lane_counts=None,run_error=None):
    prefix=config["notifications"].get("subject_prefix","Cross-Board Job Radar")
    if kind=="failure":
        subject=f"{prefix} | HEALTH ALERT"
        heading="Cross-Board Job Radar - Health Alert"
    else:
        subject=f"{prefix} | Daily Health Summary"
        heading="Cross-Board Job Radar - Daily Health Summary"
    lines=[heading,""]
    if run_at:
        lines.append(f"Run time: {_fmt(run_at,config.get('timezone','America/New_York'))}")
    if run_error:
        lines.extend(["","Run error:",str(run_error)])
    lines.extend(["",*_health_lines(health,lane_counts)])
    msg=EmailMessage(); msg["Subject"]=subject; msg.set_content("\n".join(lines))
    safe="<br>".join(html.escape(x) for x in lines)
    msg.add_alternative(f"<html><body style='font-family:Arial,sans-serif'>{safe}</body></html>",subtype="html")
    return _send_message(msg,config)
