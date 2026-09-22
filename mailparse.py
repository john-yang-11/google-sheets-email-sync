"""Turn an application email into (company, status, date).

Pure functions over the headers and snippet -- no network, no Gmail client -- so
the classification can be tested against real mail without credentials, which is
how it was tuned.

The hard part is the company. Most application mail is sent by an applicant
tracking system, so the From domain identifies Greenhouse or Workday rather than
the employer; for those the name has to come out of the subject or the opening
line, which is where the ATS puts it ("Thank you for applying to Figma").

Nothing here ever creates a spreadsheet row. A wrong or missing company just
means a date does not get filled in, never a bogus row -- which is why the
patterns can afford to be conservative and give up rather than guess.
"""

import re

# Senders that are an ATS, not an employer. A message from one of these tells us
# nothing about who the job is with, so the text has to.
ATS_DOMAINS = {
    "greenhouse-mail.io", "greenhouse.io", "lever.co", "hire.lever.co",
    "ashbyhq.com", "myworkday.com", "workday.com", "icims.com",
    "smartrecruiters.com", "gem.com", "yello.co", "ripplematch.com",
    "wayup.com", "pinpoint.email", "adp.com", "hirevue.com", "modernhire.com",
    "starred.com", "rippling.com", "oraclecloud.com", "jobvite.com",
    "taleo.net", "successfactors.com", "avature.net", "paylocity.com",
    "qualtrics.com",
}

# Rejections, most- to least-specific. Tested before the confirmations on
# purpose: almost every rejection opens by thanking you for applying, so
# checking confirmations first would label the entire rejection pile "applied".
REJECT_PATTERNS = [
    r"\bnot\s+(?:be\s+|to\s+)?(?:moving|going|move|go)\s+forward\b",
    r"\bdecided\s+not\s+to\s+(?:move|proceed|continue)\b",
    r"\bwill\s+not\s+be\s+proceeding\b",
    r"\bnot\s+be\s+progressing\b",
    r"\bregret\s+to\s+(?:inform|tell)\b",
    r"\bmoved?\s+forward\s+with\s+other\b",
    r"\bdecided\s+to\s+(?:proceed|move\s+forward)\s+with\s+other\b",
    r"\bunable\s+to\s+move\s+forward\b",
    r"\bno\s+longer\s+under\s+consideration\b",
    r"\bdo\s+not\s+meet\s+the\s+(?:eligibility|requirements)\b",
    r"\bhaven'?t\s+met\s+the\b",
    r"\bother\s+candidates\s+whose\b",
    r"\bnot\s+(?:be\s+)?selected\b",
    r"\bwe\s+are\s+sorry\s+to\b",
    r"\bat\s+this\s+time,?\s+we\s+will\s+not\b",
]

INTERVIEW_PATTERNS = [
    r"\bschedule\s+(?:an|your)\s+interview\b",
    r"\b(?:digital|video|on-?demand)\s+interview\b",
    r"\binvited?\s+to\s+(?:an?\s+)?(?:complete\s+an?\s+)?(?:on-?demand\s+|digital\s+)?interview\b",
    r"\byou\s+got\s+the\s+interview\b",
    r"\bnext\s+steps?:?\s+an?\s+interview\b",
    r"\bonline\s+assessment\b",
]

APPLIED_PATTERNS = [
    # the filler word between "your" and "application" varies by ATS:
    # "your application", "your job application", "your recent application"
    r"\breceived\s+your\s+(?:\w+\s+){0,2}application\b",
    r"\bthank(?:s|\s+you)?\s+(?:so\s+much\s+|very\s+much\s+)?for\s+applying\b",
    r"\bthank\s+you\s+for\s+your\s+application\b",
    r"\byour\s+application\s+(?:has\s+been|was)\s+received\b",
    r"\bthank\s+you\s+for\s+submitting\s+your\s+application\b",
    r"\bapplication\s+(?:has\s+been\s+)?successfully\s+submitted\b",
    # "Thanks for your interest in" and "Thank you for your interest in"
    r"\bthank(?:s|\s+you)?\s+for\s+your\s+interest\s+in\b",
    r"\bwe\s+appreciate\s+your\s+interest\b",
    r"\bcurrently\s+(?:reviewing|processing)\s+your\s+application\b",
    r"\bthank\s+you\s+for\s+taking\s+the\s+time\s+to\s+apply\b",
    r"\bapplication\s+will\s+be\s+taken\s+into\b",
]

# A capitalised name: no dot inside, so "Mercury. Your application" stops at
# "Mercury" instead of running into the next sentence.
NAME = r"[A-Z][\w&']*(?:\s[A-Z&][\w&']*){0,4}"

# "at <Company>" at the END of a subject beats "applying to <whatever>" earlier
# in it: "Thank you for applying to Software Engineering Intern, Summer 2027 at
# Plaid" names the role after "to" and the employer after "at".
SUBJECT_COMPANY = [
    r"(?i:\bat\s+)(" + NAME + r")!?\s*$",
    r"(?i:\bthank(?:s|\syou)?\s+(?:so\s+much\s+)?for\s+applying\s+(?:to|at)\s+)([^!|,\-–—:]+)",
    r"(?i:\bthank\s+you\s+for\s+your\s+(?:application|interest)\s+(?:to|in|at)\s+)([^!|,\-–—:]+)",
    r"(?i:\byour\s+)(" + NAME + r")(?i:\s+application\b)",
    r"(?i:\bupdate\s+(?:on\s+your|from)\s+)([^!|,\-–—:]+?)(?i:\s+application\b)",
    r"(?i:\bthank\s+you\s+from\s+)([^!|,\-–—:]+)",
    r"(?i:\binterview\s+with\s+(?:the\s+)?)([^!|,\-–—:(]+)",
    r"^([A-Z][\w&.'\s]{2,30}?)\s*[|\-–—]\s",          # "Talos | Thank You"
]

BODY_COMPANY = [
    r"(?i:\b(?:thanks?\s+for\s+your\s+interest\s+in|interest\s+in\s+joining"
    r"|applying\s+to|application\s+(?:to|with))\s+(?:the\s+)?)(" + NAME + r")",
    r"(?i:\b(?:role|position|internship)\s+at\s+)(" + NAME + r")",
    r"(?i:\bcareer\s+with\s+)(" + NAME + r")",
]

# Captured fragments that are never a company name.
STOPWORDS = {
    "thanks", "thank you", "thank", "your", "our", "we", "us", "you", "the",
    "application", "applying", "software engineering", "software engineer",
    "job", "role", "position", "intern", "internship", "careers", "career",
    "stay tuned", "next steps", "it", "a career", "hi", "hello", "dear",
}

# Trailing words that belong to the sentence, not the company name.
TAIL = re.compile(
    r"\b(?:and|for|the|position|role|intern(?:ship)?|team|careers?|application"
    r"|opportunit(?:y|ies)|program|summer|fall|winter|spring|20\d\d)\b.*$",
    re.IGNORECASE,
)

TLDS = {"com", "co", "org", "net", "io", "ai", "jobs", "careers", "inc", "us",
        "uk", "edu", "gov", "dev", "app", "cloud", "email"}

# Subdomain labels that are infrastructure, never the company.
GENERIC_LABELS = {"mail", "email", "people", "my", "www", "careers", "recruiting",
                  "talent", "hire", "apply", "no-reply", "noreply", "qualtrics",
                  "workflow", "notifications"}


def domain_of(sender: str) -> str:
    m = re.search(r"@([\w.\-]+)", sender or "")
    return (m.group(1) if m else "").lower().strip(".")


def is_ats(sender: str) -> bool:
    d = domain_of(sender)
    return any(d == a or d.endswith("." + a) for a in ATS_DOMAINS)


def company_from_domain(sender: str) -> str:
    """'no-reply@mail.amazon.jobs' -> 'Amazon'. Empty for an ATS.

    Takes the last label before the TLD, not the longest one: people.nokia.com is
    Nokia, and picking the longest label made it "People".
    """
    d = domain_of(sender)
    if not d or is_ats(sender):
        return ""
    parts = d.split(".")
    while len(parts) > 1 and parts[-1] in TLDS:
        parts.pop()
    while len(parts) > 1 and parts[-1] in GENERIC_LABELS:
        parts.pop()
    return parts[-1].replace("-", " ").title() if parts else ""


def company_from_ats_tenant(sender: str) -> str:
    """Workday and iCIMS put the employer in the local part: visa@myworkday.com.

    Last resort only. Greenhouse, Lever and Ashby send from a shared no-reply
    address whose local part says nothing at all about the employer.
    """
    d = domain_of(sender)
    if not any(d.endswith(x) for x in ("myworkday.com", "icims.com", "adp.com")):
        return ""
    local = re.split(r"[+@]", sender or "")[0]
    local = re.sub(r"^(?:no-?reply|donotreply|do-not-reply)\.?", "", local, flags=re.I)
    local = re.sub(r"\.(?:hr|autoreply)$", "", local, flags=re.I)
    local = re.sub(r"(?:careers?|recruiting|talent|jobs|hr)$", "", local, flags=re.I)
    local = local.replace("-", " ").replace(".", " ").strip()
    return local.title() if len(local) > 1 else ""


def clean_company(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "")).strip(" .,!|-–—:\t")
    s = TAIL.sub("", s).strip(" .,!|-–—:")
    s = re.sub(r"^(?:the|our|your|a)\s+", "", s, flags=re.IGNORECASE)
    if s.lower() in STOPWORDS or not (1 < len(s) <= 45):
        return ""
    return s


def _from_text(subject: str, snippet: str) -> str:
    for pat in SUBJECT_COMPANY:
        if (m := re.search(pat, subject or "")) and (c := clean_company(m.group(1))):
            return c
    for pat in BODY_COMPANY:
        if (m := re.search(pat, snippet or "")) and (c := clean_company(m.group(1))):
            return c
    return ""


def _squash(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def company_of(sender: str, subject: str, snippet: str = "") -> str:
    """Best guess at the employer, or "" when nothing is confident enough.

    A company's own domain is the most reliable signal, with two exceptions:
    when it is an abbreviation the text spells out (ea.com is Electronic Arts),
    and when the text writes the same name more legibly ("CME Group" vs the
    cmegroup.com domain) -- a hand-written sheet row will match the spaced form.
    """
    dom = company_from_domain(sender)
    txt = _from_text(subject, snippet)
    if dom and txt and _squash(dom) == _squash(txt):
        return txt
    if dom and len(dom) > 3:
        return dom
    return txt or dom or company_from_ats_tenant(sender)


def status_of(text: str) -> str | None:
    """'rejected' | 'interview' | 'applied' | None."""
    t = re.sub(r"\s+", " ", text or "")
    for group, label in ((REJECT_PATTERNS, "rejected"),
                         (INTERVIEW_PATTERNS, "interview"),
                         (APPLIED_PATTERNS, "applied")):
        for pat in group:
            if re.search(pat, t, re.IGNORECASE):
                return label
    return None


def parse(sender: str, subject: str, snippet: str, date: str) -> dict | None:
    """One email -> {company, status, date}, or None if it is not an application."""
    status = status_of(f"{subject} {snippet}")
    if not status:
        return None
    company = company_of(sender, subject, snippet)
    if not company:
        return None
    return {"company": company, "status": status, "date": (date or "")[:10]}
