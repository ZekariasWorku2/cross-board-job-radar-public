#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re
from pathlib import Path
from zoneinfo import ZoneInfo
from crossboard.engine import load_state, scan, pending_for_lane, mark_sent, mark_audit_delivery
from crossboard.emailer import send_lane_email, send_health_email
from crossboard.model import utc_now_iso, parse_timestamp

CONFIG_PATH=Path("config.json"); STATE_PATH=Path("state/state.json"); OUTPUT_PATH=Path("output/latest.json")

def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n",encoding="utf-8"); tmp.replace(path)

def _stable_error_token(error):
    text=str(error or "").strip()
    if not text:
        return ""
    error_type=text.split(":",1)[0].strip() if ":" in text else "Error"
    http_status=re.search(r"\b([1-5]\d{2})\b\s+(?:Client|Server) Error\b",text,re.IGNORECASE)
    if http_status:
        return f"{error_type}:HTTP:{http_status.group(1)}"
    return text

def _failure_signature(source_errors,run_error=None):
    stable_source_errors={source:_stable_error_token(error) for source,error in (source_errors or {}).items()}
    payload={"source_errors":stable_source_errors,"run_error":_stable_error_token(run_error)}
    return json.dumps(payload,sort_keys=True,separators=(",",":"))

def _local_day_hour(now,tz_name):
    dt=parse_timestamp(now).astimezone(ZoneInfo(tz_name))
    return dt.date().isoformat(),dt.hour

def _lane_counts(result):
    return {lane:len((result.get("lanes",{}).get(lane,{}) or {}).get("qualifying",[]) or []) for lane in ("data_engineer","sql_developer")}

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--notify",action="store_true"); parser.add_argument("--dry-run",action="store_true"); args=parser.parse_args()
    config=json.loads(CONFIG_PATH.read_text(encoding="utf-8")); state=load_state(STATE_PATH); now=utc_now_iso(); notify_state=state.setdefault("notification_state",{})
    notifications=config.get("notifications",{})

    try:
        result=scan(config,state,now)
    except Exception as exc:
        health={"sources_configured":len([1 for c in config.get("sources",{}).values() if c.get("enabled")]),"sources_ok":0,"source_errors":{"run":f"{type(exc).__name__}: {exc}"},"raw_records":0,"unique_jobs":0,"page_verifications":0,"page_verification_candidates":0}
        result={"run_at":now,"health":health,"source_stats":{},"lanes":{},"email":{}}
        if args.notify and notifications.get("send_failure_alerts",True):
            sig=_failure_signature({},exc)
            if notify_state.get("last_failure_signature")!=sig:
                delivery=send_health_email("failure",health,config,run_at=now,run_error=f"{type(exc).__name__}: {exc}")
                result["email"]["health_alert"]=delivery
                if delivery.get("sent"):
                    notify_state["last_failure_signature"]=sig
                    notify_state["last_failure_alert_at"]=now
        if not args.dry_run:
            write_json(STATE_PATH,state); write_json(OUTPUT_PATH,result)
        print(json.dumps({"run_at":now,"health":health,"email":result["email"],"fatal_error":f"{type(exc).__name__}: {exc}"},indent=2))
        return 1

    result["email"]={}; delivery_failed=False
    if args.notify:
        # Job-alert emails: only send when at least one new qualifying job exists.
        for lane in ("data_engineer","sql_developer"):
            jobs=pending_for_lane(state,lane,int(notifications.get("max_jobs_per_email",50)))
            if jobs:
                delivery=send_lane_email(lane,jobs,result["health"],config); result["email"][lane]=delivery
                if delivery.get("sent"):
                    mark_sent(state,lane,jobs,now)
                    mark_audit_delivery(state,result,lane,jobs,"emailed")
                else:
                    mark_audit_delivery(state,result,lane,jobs,"email_failed",delivery.get("error"))
                    delivery_failed=True
            else:
                result["email"][lane]={"sent":False,"configured":True,"skipped":"zero qualifying jobs"}

        # Immediate health alert for a new/changed failure condition. Repeated identical
        # failures do not generate another email every two hours.
        source_errors=result.get("health",{}).get("source_errors") or {}
        if source_errors and notifications.get("send_failure_alerts",True):
            sig=_failure_signature(source_errors)
            if notify_state.get("last_failure_signature")!=sig:
                delivery=send_health_email("failure",result["health"],config,run_at=now,lane_counts=_lane_counts(result))
                result["email"]["health_alert"]=delivery
                if delivery.get("sent"):
                    notify_state["last_failure_signature"]=sig
                    notify_state["last_failure_alert_at"]=now
                else:
                    delivery_failed=True
        elif not source_errors:
            # Clearing the signature means the same problem will alert again if it
            # disappears and later returns.
            notify_state.pop("last_failure_signature",None)

        # One daily health summary, sent on the first normal run at/after the configured
        # local hour. With a two-hour workflow cadence this is approximately that hour.
        if notifications.get("daily_health_summary",True):
            tz_name=config.get("timezone","America/New_York")
            local_day,local_hour=_local_day_hour(now,tz_name)
            health_hour=int(notifications.get("daily_health_hour_local",8))
            if local_hour>=health_hour and notify_state.get("last_daily_health_date")!=local_day:
                delivery=send_health_email("daily",result["health"],config,run_at=now,lane_counts=_lane_counts(result))
                result["email"]["daily_health"]=delivery
                if delivery.get("sent"):
                    notify_state["last_daily_health_date"]=local_day
                    notify_state["last_daily_health_at"]=now
                else:
                    delivery_failed=True

    if not args.dry_run:
        write_json(STATE_PATH,state); write_json(OUTPUT_PATH,result)
    print(json.dumps({"run_at":now,"health":result["health"],"email":result["email"],"data_engineer_pending":len(pending_for_lane(state,"data_engineer")),"sql_developer_pending":len(pending_for_lane(state,"sql_developer"))},indent=2))
    return 1 if delivery_failed else 0
if __name__=="__main__": raise SystemExit(main())
