"""Evaluate a complete JD against anonymized evidence from the uploaded resume.

This deterministic evaluator is explainable; it is not a language-model semantic
assessment. The profile does not store the user's name or contact details.
"""
import json
import os
import re
from pathlib import Path
from .model import clean, token_set, jaccard

PROFILE_PATH = Path(os.environ.get("JOB_RADAR_PROFILE_PATH") or (Path(__file__).resolve().parent.parent / "profiles" / "data_engineer.json"))

def _phrase(text, phrase):
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(clean(phrase)) + r"(?![a-z0-9])", text))

def compare_resume(job):
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    desc = clean(" ".join(str(job.get(k) or "") for k in ("page_description", "description")))
    skill_hits = []
    for group, aliases in profile["skills"].items():
        evidence = [p for p in aliases if _phrase(desc, p)]
        if evidence:
            skill_hits.append({"group": group, "jd_terms": evidence[:6]})
    tokens = token_set(desc)
    ranked = sorted(((jaccard(tokens, token_set(e)), e) for e in profile["experience_evidence"]), reverse=True)
    evidence = [{"resume_experience": e, "overlap": round(s, 3)} for s, e in ranked[:3] if s >= 0.035]
    # Grade breadth of verified resume/JD intersections. The score is bounded and
    # is an evidence indicator, never a substitute for hard eligibility filters.
    covered = len(skill_hits)
    score = min(100, 35 + min(54, covered * 9) + (8 if evidence and evidence[0]["overlap"] >= 0.12 else 0))
    if not desc:
        score = 0
    return {"resume_fit_score": score, "resume_skill_matches": skill_hits,
            "resume_experience_matches": evidence, "resume_evidence_available": bool(desc)}
