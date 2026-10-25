#!/usr/bin/env python3
"""Report on a real scan without sending emails or persisting alert history."""
from __future__ import annotations
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crossboard.engine import load_state, scan
from crossboard.model import utc_now_iso

OUT = ROOT / "dry-run-results"

def compact(job):
    return {key: job.get(key) for key in (
        "title", "company", "source", "sources_seen", "url", "apply_url",
        "page_source_url", "validation_class", "validation_score",
        "resume_fit_score", "resume_skill_matches", "validation_reject_reason",
        "freshness_decision", "freshness_reason", "freshness_confidence",
        "job_notification_class", "verification_needed",
        "earliest_published_at", "freshness_evidence"
    )}

def publish_summary(markdown):
    OUT.mkdir(exist_ok=True)
    (OUT / "summary.md").write_text(markdown + "\n", encoding="utf-8")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as writer:
            writer.write(markdown + "\n")
    print(markdown, flush=True)

def main():
    OUT.mkdir(exist_ok=True)
    now = utc_now_iso()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    state = load_state(ROOT / "state/state.json")
    baseline = not bool(state.get("jobs"))
    try:
        result = scan(config, state, now)
    except Exception as exc:
        publish_summary("# Cross-Board dry run failed\n\n- Error: "
                        + type(exc).__name__ + ": " + str(exc)
                        + "\n- Emails sent: 0\n- Saved state modified: No")
        raise
    health = result.get("health") or {}
    lanes = {}
    for lane in ("data_engineer", "sql_developer"):
        entry = (result.get("lanes") or {}).get(lane) or {}
        qualifying = entry.get("qualifying") or []
        rejected = entry.get("rejected") or []
        reasons = Counter(j.get("validation_reject_reason") or
                          j.get("freshness_reason") or "Not alertable" for j in rejected)
        lanes[lane] = {
            "funnel": entry.get("funnel") or {},
            "qualifying": [compact(j) for j in qualifying],
            "manual_review": [compact(j) for j in entry.get("manual_review") or []],
            "rejected": [compact(j) for j in rejected],
            "rejection_reason_counts": reasons.most_common(),
        }
    report = {
        "mode": "DRY_RUN_NO_EMAIL",
        "run_at_utc": now,
        "emails_sent": 0,
        "saved_radar_state_modified": False,
        "initial_baseline": baseline,
        "health": health,
        "source_stats": result.get("source_stats") or {},
        "lanes": lanes,
        "discovery_audit": [
            {key: row.get(key) for key in (
                "source", "source_job_id", "title", "company", "url",
                "source_published_at", "page_verification", "page_error",
                "final_status", "final_reason", "lane_decisions",
            )} for row in (result.get("discovery_audit") or [])
        ],
    }
    (OUT / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8"
    )
    lines = [
        "# Cross-Board Job Radar: Live Discovery Dry Run", "",
        "- Run (UTC): " + now,
        "- Emails sent: **0**",
        "- Saved radar state modified: **No**",
        "- Initial baseline: **" + ("Yes" if baseline else "No") + "**",
        "- Sources successful: **" + str(health.get("sources_ok", 0)) +
          "/" + str(health.get("sources_configured", 0)) + "**",
        "- Raw discovery hits: **" + str(health.get("raw_query_hits", 0)) + "**",
        "- Unique job clusters: **" + str(health.get("unique_jobs", 0)) + "**",
        "- Page verifications: **" + str(health.get("page_verifications", 0)) + "**", "",
        "| Lane | Qualified | Needs Manual Verification | Rejected clusters |",
        "|---|---:|---:|---:|"
    ]
    for name, entry in lanes.items():
        confirmed=sum(j.get("job_notification_class")=="qualified" for j in entry["qualifying"])
        lines.append("| " + name + " | " + str(confirmed) + " | " +
                     str(len(entry["manual_review"])) + " | " + str(len(entry["rejected"])) + " |")
    for name, entry in lanes.items():
        lines.extend(["", "## " + name + ": would-be alerts"])
        if not entry["qualifying"]:
            lines.append("No new qualifying jobs.")
        for j in entry["qualifying"][:30]:
            title = str(j.get("title") or "").replace("|", " ").replace("\n", " ")
            company = str(j.get("company") or "").replace("|", " ").replace("\n", " ")
            lines.append("- " + title + " (" + company + "), score " +
                         str(j.get("validation_score")) + ", original-freshness " +
                         str(j.get("freshness_decision"))+"; "+str(j.get("job_notification_class"))+"; "+str(j.get("verification_needed")))
        lines.extend(["", "Top rejection reasons:"])
        for reason, count in entry["rejection_reason_counts"][:12]:
            lines.append("- " + str(count) + " x " +
                         str(reason).replace("\n", " ")[:150])
    if health.get("source_errors"):
        lines.extend(["", "## Source errors"])
        for name, value in sorted(health["source_errors"].items()):
            lines.append("- " + name + ": " + str(value)[:200])
    lines.extend(["", "Download the report.json artifact for all candidate decisions and evidence."])
    publish_summary("\n".join(lines))
    return 0 if health.get("sources_ok", 0) else 1

if __name__ == "__main__":
    raise SystemExit(main())
