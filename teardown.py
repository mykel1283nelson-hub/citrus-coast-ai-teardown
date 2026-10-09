"""
Citrus Coast AI — self-serve teardown engine (Model B, v1).
Deterministic checks only: $0 marginal cost per teardown (no LLM inference).
Business URL in -> structured AI-visibility teardown out.

Checks mirror what the manual teardowns (teardown-01/02) found by hand:
dead sites, missing mobile support, no tap-to-call, stale social, etc.
"""
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, field, asdict
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

USER_AGENT = "CitrusCoastAI-TeardownBot/1.0 (+https://citruscoast.ai)"
TIMEOUT = 12.0


def _http_client():
    """httpx client that works behind the runtime egress proxy.
    (trust_env=False avoids an httpx no_proxy IPv6 parsing bug;
    proxy URL is read from env at runtime, never hardcoded.)"""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    kwargs = {"timeout": TIMEOUT, "follow_redirects": True,
              "headers": {"User-Agent": USER_AGENT}}
    if proxy:
        kwargs.update({"trust_env": False, "proxy": proxy,
                       "verify": "/etc/ssl/certs/ca-certificates.crt"})
    return httpx.Client(**kwargs)


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self._in_title = False
        self.meta = {}
        self.tel_links = 0
        self.fb_links = 0
        self.forms = 0
        self.text_len = 0
        self.scripts_wp = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name:
                self.meta[name] = a.get("content", "")
        elif tag == "a":
            href = (a.get("href") or "").lower()
            if href.startswith("tel:"):
                self.tel_links += 1
            if "facebook.com" in href:
                self.fb_links += 1
        elif tag == "form":
            self.forms += 1
        elif tag == "script":
            src = (a.get("src") or "").lower()
            if "wp-" in src or "wordpress" in src:
                self.scripts_wp = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        self.text_len += len(data.strip())


@dataclass
class Finding:
    check: str
    status: str  # "pass" | "warn" | "fail"
    detail: str


@dataclass
class Teardown:
    business_name: str
    url: str
    final_url: str = ""
    findings: list = field(default_factory=list)
    score: int = 0  # 0-100
    fixes_30_day: list = field(default_factory=list)
    generated_by: str = "Citrus Coast AI Teardown Engine v1"

    def to_dict(self):
        d = asdict(self)
        return d


def _finding(check, status, detail):
    return Finding(check=check, status=status, detail=detail)


def run_teardown(business_name: str, url: str, location: str = "") -> Teardown:
    t = Teardown(business_name=business_name.strip(), url=url.strip())
    if location:
        t.business_name = f"{t.business_name} — {location.strip()}"

    raw = t.url
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.hostname or ""

    # --- 1. DNS / TCP ---
    try:
        socket.getaddrinfo(host, 443)
        t.findings.append(_finding("DNS", "pass", f"{host} resolves."))
    except Exception as e:
        t.findings.append(_finding("DNS", "fail", f"{host} does not resolve ({e}). Site is unreachable."))
        t.score = 5
        t.fixes_30_day = [
            "Get the domain resolving again or move to a working domain — right now customers hit a dead end.",
            "Put up a clean one-page site with tap-to-call so nobody ever hits a dead end again.",
            "Claim and correct the Google Business Profile so it points somewhere real.",
        ]
        return t

    # --- 2. HTTPS fetch ---
    status, final, elapsed, headers, body, err = None, raw, 0, {}, b"", None
    t0 = time.time()
    try:
        with _http_client() as c:
            r = c.get(raw)
            status, final, headers, body = r.status_code, str(r.url), r.headers, r.content
    except Exception as e:
        err = str(e)
    elapsed = round(time.time() - t0, 1)
    t.final_url = final

    if err or status is None:
        t.findings.append(_finding("Site reachable", "fail",
            f"Could not load the site ({err or 'no response'}). Customers see an error page or nothing."))
        t.score = 8
        t.fixes_30_day = [
            "Fix hosting so the site loads — every failed load is a customer calling a competitor.",
            "Add HTTPS so browsers stop showing 'not secure' warnings.",
            "Put a tap-to-call phone number above the fold.",
        ]
        return t

    if status >= 500:
        t.findings.append(_finding("Site reachable", "fail", f"Server error (HTTP {status}). The site is broken right now."))
    elif status >= 400:
        t.findings.append(_finding("Site reachable", "fail", f"Page not found (HTTP {status}). The link customers click is dead."))
    elif status >= 300:
        t.findings.append(_finding("Site reachable", "warn", f"Redirects to {final}."))
    else:
        t.findings.append(_finding("Site reachable", "pass", f"Loads fine (HTTP {status}, {elapsed}s, {len(body)//1024} KB)."))

    # --- 3. HTTPS ---
    if final.startswith("https://"):
        t.findings.append(_finding("HTTPS", "pass", "Secure connection. No browser warnings."))
    else:
        t.findings.append(_finding("HTTPS", "fail", "No HTTPS — browsers flag the site 'Not Secure'. Customers bounce."))

    # --- 4+. HTML analysis ---
    p = _MetaParser()
    try:
        p.feed(body.decode("utf-8", errors="ignore")[:500_000])
    except Exception:
        pass

    title = p.title.strip()
    if len(title) >= 10:
        t.findings.append(_finding("Page title", "pass", f'Title set: "{title[:70]}". Shows up correctly in Google.'))
    elif title:
        t.findings.append(_finding("Page title", "warn", f'Title is only "{title}" — too short to describe the business in search results.'))
    else:
        t.findings.append(_finding("Page title", "fail", "No page title. Google shows a bare URL instead of the business name."))

    desc = p.meta.get("description", "").strip()
    if len(desc) >= 50:
        t.findings.append(_finding("Meta description", "pass", "Search-result snippet is written. Controls what customers read on Google."))
    elif desc:
        t.findings.append(_finding("Meta description", "warn", "Meta description is very short — Google will write its own snippet."))
    else:
        t.findings.append(_finding("Meta description", "fail", "No meta description. Google invents the snippet — usually badly."))

    if "viewport" in p.meta:
        t.findings.append(_finding("Mobile-friendly", "pass", "Mobile viewport configured. Most customers are on phones."))
    else:
        t.findings.append(_finding("Mobile-friendly", "fail", "No mobile viewport tag — the site likely renders tiny/unusable on phones."))

    if p.tel_links > 0:
        t.findings.append(_finding("Tap-to-call", "pass", f"{p.tel_links} tap-to-call phone link(s). One tap and the customer is calling."))
    else:
        t.findings.append(_finding("Tap-to-call", "fail", "No tap-to-call phone link. A phone customer has to memorize or copy the number."))

    if p.fb_links > 0:
        t.findings.append(_finding("Facebook linked", "pass", "Facebook page is linked from the site."))
    else:
        t.findings.append(_finding("Facebook linked", "warn", "No Facebook link found on the site — social proof is disconnected."))

    if p.forms > 0:
        t.findings.append(_finding("Contact form", "pass", f"{p.forms} form(s) on the page — customers can request a quote."))
    else:
        t.findings.append(_finding("Contact form", "warn", "No contact/quote form found — the only path is calling."))

    # phone/address visible in text?
    text = body.decode("utf-8", errors="ignore")
    phone_hit = re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
    if phone_hit:
        t.findings.append(_finding("Phone visible", "pass", "A phone number appears in the page content."))
    else:
        t.findings.append(_finding("Phone visible", "fail", "No phone number found in the page content."))

    # --- score ---
    weights = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    vals = [weights[f.status] for f in t.findings]
    t.score = round(100 * sum(vals) / len(vals)) if vals else 0

    # --- 30-day fixes mapped from failures ---
    fix_map = {
        "Site reachable": "Fix hosting so the site loads reliably — every failed load is a customer calling a competitor.",
        "HTTPS": "Add HTTPS so browsers stop showing 'Not Secure' warnings.",
        "Page title": "Write a real page title (business name + service + city) so Google shows the business properly.",
        "Meta description": "Write the search-result snippet — one or two sentences that make a customer click.",
        "Mobile-friendly": "Make the site render properly on phones — most local customers never open a laptop.",
        "Tap-to-call": "Add a tap-to-call phone button above the fold — one tap from search to ringing phone.",
        "Facebook linked": "Link the Facebook page from the site and post weekly — a dead social presence reads as a dead business.",
        "Contact form": "Add a simple quote-request form so after-hours customers don't bounce.",
        "Phone visible": "Put the phone number in the page content where customers (and Google) can see it.",
    }
    fixes = []
    for f in t.findings:
        if f.status == "fail" and f.check in fix_map and fix_map[f.check] not in fixes:
            fixes.append(fix_map[f.check])
    # pad with standard 30-day items if few failures
    standard = [
        "Claim and correct the Google Business Profile so it points at the real site.",
        "Set up weekly Facebook posts so the business looks alive.",
    ]
    for s in standard:
        if len(fixes) < 4 and s not in fixes:
            fixes.append(s)
    t.fixes_30_day = fixes[:5]

    return t


if __name__ == "__main__":
    import json, sys
    name = sys.argv[1] if len(sys.argv) > 1 else "Demo Business"
    url = sys.argv[2] if len(sys.argv) > 2 else "https://example.com"
    loc = sys.argv[3] if len(sys.argv) > 3 else ""
    print(json.dumps(run_teardown(name, url, loc).to_dict(), indent=2))
