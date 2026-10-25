from __future__ import annotations

import re
from .model import clean

# Sources whose discovery request itself is explicitly constrained to the United States.
US_SCOPED_SOURCES = {
    "linkedin", "indeed", "himalayas", "jobicy", "jobopportunities", "ayla"
}

BAD_WORKPLACE_VALUES = {
    "hybrid", "onsite", "on-site", "on site", "in-office", "in office", "office"
}

US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia", "washington dc", "washington, dc"
)

NON_US_COUNTRIES = (
    "canada", "india", "united kingdom", "uk", "germany", "poland", "philippines", "australia",
    "pakistan", "mexico", "brazil", "argentina", "chile", "colombia", "peru", "spain", "france",
    "italy", "ireland", "netherlands", "belgium", "sweden", "norway", "denmark", "finland",
    "portugal", "switzerland", "austria", "czech", "romania", "hungary", "ukraine", "serbia",
    "croatia", "greece", "turkey", "israel", "egypt", "south africa", "nigeria", "kenya",
    "singapore", "japan", "china", "taiwan", "south korea", "vietnam", "thailand", "malaysia",
    "indonesia", "new zealand", "latin america", "latam", "emea", "apac"
)

OFFICE_PATTERNS = (
    r"\b(?:in[- ]office|on[- ]?site|onsite)\s+(?:role|position|job|work|schedule|requirement)\b",
    r"\b(?:role|position|job|work location|work arrangement|work model|schedule)\s*(?:is|:|will be)\s*(?:an?\s+)?(?:in[- ]office|on[- ]?site|onsite|hybrid)\b",
    r"\bhybrid\s+(?:role|position|job|schedule|model|work|arrangement|environment|opportunity)\b",
    r"\b(?:[1-5]|one|two|three|four|five)\s+days?\s+(?:per|a)\s+week\b.{0,45}\b(?:office|on[- ]?site|onsite)\b",
    r"\b(?:office|on[- ]?site|onsite)\b.{0,45}\b(?:[1-5]|one|two|three|four|five)\s+days?\s+(?:per|a)\s+week\b",
    r"\b(?:[1-9]|one|two|three|four|five|six|seven|eight|nine)\s+days?\s+(?:per|a)\s+month\b.{0,45}\b(?:office|on[- ]?site|onsite)\b",
    r"\bmust\s+(?:work|report|be|come|commute).{0,60}\b(?:on[- ]?site|onsite|in[- ]office|in the office|office)\b",
    r"\brequired\s+to\s+(?:work|report|be|come).{0,60}\b(?:on[- ]?site|onsite|in[- ]office|in the office|office)\b",
    r"\bremote\b.{0,80}\b(?:required|must|with)\b.{0,60}\b(?:office|on[- ]?site|onsite)\b",
    r"\bremote\s+(?:after|following)\s+(?:training|onboarding|orientation)\b",
    r"\b(?:not|no)\s+(?:a\s+)?remote\b",
    r"\bremote\s+work\s+(?:is\s+)?not\s+(?:available|offered|permitted|allowed)\b",
    r"\bmust\s+be\s+within\s+(?:commuting|commutable)\s+distance\b",
)

REMOTE_PATTERNS = (
    r"\b100\s*%\s+remote\b",
    r"\bfully\s+remote\b",
    r"\bremote[- ]first\b",
    r"\bremote\s+(?:role|position|job|opportunity|work|employee|employees)\b",
    r"\b(?:role|position|job|opportunity)\s+(?:is\s+|will\s+be\s+)?remote\b",
    r"\bwork(?:ing)?\s+remotely\b",
    r"\bwork[- ]from[- ]home\b",
    r"\btelecommut(?:e|ing|er|ers)?\b",
    r"\bremote\s+(?:within|in|across|throughout|from)\s+(?:the\s+)?(?:united states|u\.s\.|usa|us)\b",
)

US_PATTERNS = (
    r"\bunited states\b", r"\bu\.s\.\b", r"\busa\b", r"\bus[- ]based\b",
    r"\bremote\s+(?:within|in|across|throughout|from)\s+(?:the\s+)?us\b",
)


def _has(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


def _state_location(text):
    if any(name in text for name in US_STATE_NAMES):
        return True
    return bool(re.search(
        r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b",
        text,
        re.I,
    ))


def _explicit_non_us(location):
    for country in NON_US_COUNTRIES:
        if re.search(r"(?<![a-z])" + re.escape(country) + r"(?![a-z])", location, re.I):
            if not re.search(r"\bunited states\b|\bu\.s\.?\b|\busa\b|\bus\b", location, re.I):
                return True
    return False


def _virginia_excluded(text):
    if re.search(r"(?:cannot|not|excluding|exclude|except).{0,35}\bvirginia\b", text, re.I):
        return True
    if re.search(r"\b(?:eligible|approved|authorized|following)\s+states?\b", text, re.I):
        # State-list language is a hard eligibility gate. If Virginia is not named, do not alert.
        if not re.search(r"\bvirginia\b|(?:^|[,/ ]+)VA(?:[,/ .]|$)", text, re.I):
            return True
    restriction = re.search(
        r"\bmust\s+(?:be\s+)?(?:reside|live|be located|be based|work)\s+in\s+([^.;\n]{1,180})",
        text,
        re.I,
    )
    if restriction:
        value = clean(restriction.group(1))
        nationwide = re.search(r"\bunited states\b|\bu\.s\.\b|\busa\b|\bnationwide\b", value, re.I)
        virginia = re.search(r"\bvirginia\b|(?:^|[,/ ]+)va(?:[,/ .]|$)", value, re.I)
        if not nationwide and not virginia:
            return True
    return False


def remote_status(job):
    """'reject' for explicit location/work-arrangement conflicts; 'review' for unknowns."""
    board_location=clean(job.get("location"))
    page_location=clean(job.get("page_location"))
    board_description=clean(job.get("description"))
    page_description=clean(job.get("page_description"))
    source=clean(job.get("source"))
    board_workplace=clean(job.get("workplace_type"))
    page_workplace=clean(job.get("page_workplace_type"))
    page_text=clean(" ".join(x for x in (page_location,page_description) if x))
    board_text=clean(" ".join(x for x in (board_location,board_description) if x))
    all_text=clean(" ".join((board_text,page_text)))
    if page_workplace in BAD_WORKPLACE_VALUES or board_workplace in BAD_WORKPLACE_VALUES:
        return "reject","Employer or board metadata explicitly indicates hybrid/on-site work"
    if _has(OFFICE_PATTERNS,all_text):
        return "reject","Job description explicitly requires hybrid/on-site attendance"
    location=page_location or board_location
    if _explicit_non_us(location):
        return "reject","Posting location is outside the United States"
    if _virginia_excluded(all_text):
        return "reject","Remote location restriction explicitly excludes Virginia"
    needs=[]
    if page_text:
        remotely_confirmed=(page_workplace=="remote" or _has(REMOTE_PATTERNS,page_text)
                or bool(re.search(r"\bremote\b",page_location)))
    else:
        remotely_confirmed=(board_workplace=="remote" and
                (_has(REMOTE_PATTERNS,board_text) or bool(re.search(r"\bremote\b",board_location))))
    if not remotely_confirmed:
        needs.append("Fully remote work arrangement is not confirmed")
    us_evidence=page_text if page_text else board_text
    us_confirmed=bool(_has(US_PATTERNS,us_evidence) or _state_location(location)
        or (not page_text and source in US_SCOPED_SOURCES)
        or (page_text and source in US_SCOPED_SOURCES and not location))
    if not us_confirmed:
        needs.append("United States eligibility is not confirmed")
    if needs:
        return "review","; ".join(needs)
    return "ok","Fully remote U.S. eligibility confirmed"
