"""
Citrus Coast AI — Model B server.
- GET  /                  web front end (free teardown form, rate-limited)
- POST /teardown          human tier: free, 3/day per IP -> HTML report + $499 CTA
- POST /api/teardown      x402 tier: $0.50 USDC on Base per call -> JSON report
- GET  /.well-known/x402  discovery for x402 indexers
- GET  /skill.md, /llms.txt  agent-readable docs

$0 to run: deterministic teardown engine (no LLM inference per call).
Payments settle to the venture Base wallet; server never holds keys.
"""
import base64
import json
import time
from collections import defaultdict

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from teardown import run_teardown

# --- config ---
PAY_TO = "0x1c595F805edBCBa5873DeB6A9182cD84fC5Dae3A"  # venture Base wallet
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NETWORK = "eip155:8453"  # Base mainnet
PRICE_ATOMIC = "500000"  # $0.50 USDC (6 decimals)
STRIPE_LINK = "https://buy.stripe.com/3cI00l6IvdZufAUgU2eUU00"
FACILITATOR_URL = "https://x402.org/facilitator"

app = FastAPI(title="Citrus Coast AI Teardown API")

# --- simple rate limit: 3 free teardowns / IP / day ---
_hits = defaultdict(list)
FREE_PER_DAY = 3


def _rate_ok(ip: str) -> bool:
    now = time.time()
    _hits[ip] = [t for t in _hits[ip] if now - t < 86400]
    if len(_hits[ip]) >= FREE_PER_DAY:
        return False
    _hits[ip].append(now)
    return True


def _payment_requirements() -> dict:
    return {
        "scheme": "exact",
        "network": NETWORK,
        "asset": USDC_BASE,
        "amount": PRICE_ATOMIC,
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 300,
        "extra": {
            "name": "AI Visibility Teardown",
            "description": "Deterministic AI-visibility teardown: 11 checks on a business website (reachability, HTTPS, SEO basics, mobile, tap-to-call, contact paths) with scored findings and 30-day fixes. Under 500 chars.",
        },
    }


def _report_html(t) -> str:
    rows = ""
    color = {"pass": "#1e6b3a", "warn": "#b7791f", "fail": "#b3261e"}
    icon = {"pass": "✓", "warn": "!", "fail": "✕"}
    for f in t.findings:
        rows += (f"<div style='border-left:4px solid {color[f.status]};padding:10px 12px;margin:8px 0;"
                 f"background:#f8faf8;'><b>{icon[f.status]} {f.check}</b><br>"
                 f"<span style='color:#33475c;font-size:14px'>{f.detail}</span></div>")
    fixes = "".join(f"<li style='margin:6px 0'>{x}</li>" for x in t.fixes_30_day)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Teardown: {t.business_name} — Citrus Coast AI</title></head>
<body style="font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;margin:0;background:#f2f5f2;color:#1a1a1a">
<div style="max-width:640px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(160deg,#123524,#1e6b3a);color:#fff;border-radius:14px;padding:28px 24px;text-align:center">
<div style="font-size:12px;letter-spacing:2px;opacity:.8">CITRUS COAST AI — FREE TEARDOWN</div>
<h1 style="margin:10px 0 4px;font-size:24px">{t.business_name}</h1>
<div style="font-size:13px;opacity:.85;word-break:break-all">{t.final_url or t.url}</div>
<div style="font-size:52px;font-weight:800;margin:12px 0 0;color:#9fe870">{t.score}<span style="font-size:20px">/100</span></div>
<div style="font-size:13px;opacity:.85">AI-visibility score</div></div>
<h2 style="color:#123524;margin:22px 0 6px">What we found</h2>{rows}
<h2 style="color:#123524;margin:22px 0 6px">What we'd fix in the first 30 days</h2>
<ul style="color:#33475c;font-size:15px;line-height:1.5">{fixes}</ul>
<div style="background:#123524;color:#fff;border-radius:14px;padding:26px 24px;text-align:center;margin-top:24px">
<h2 style="margin:0 0 8px">We do all of this for you.</h2>
<p style="opacity:.85;font-size:15px;margin:0 0 18px">A clean site, weekly posts, review responses, Google listing upkeep, and a weekly report. You never touch a computer.</p>
<a href="{STRIPE_LINK}" style="display:block;background:#9fe870;color:#123524;font-weight:800;font-size:19px;padding:15px;border-radius:10px;text-decoration:none">$499/mo — hire your AI employee</a>
<p style="font-size:12px;opacity:.65;margin:12px 0 0">Month to month. No contract. Cancel anytime.</p></div>
<p style="font-size:11px;color:#8a9a8a;text-align:center;margin-top:18px">Deterministic audit — 11 live checks, no guesswork. {t.generated_by}</p>
</div></body></html>"""


INDEX_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Free AI-Visibility Teardown — Citrus Coast AI</title></head>
<body style="font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;margin:0;background:#f2f5f2;color:#1a1a1a">
<div style="max-width:600px;margin:0 auto;padding:24px">
<div style="background:linear-gradient(160deg,#123524,#1e6b3a);color:#fff;border-radius:14px;padding:34px 26px;text-align:center">
<div style="font-size:12px;letter-spacing:2px;opacity:.8">CITRUS COAST AI</div>
<h1 style="margin:10px 0">Is your business invisible to AI?</h1>
<p style="opacity:.88;font-size:15px;line-height:1.55;margin:0">Customers ask AI assistants for local recommendations every day. We run 11 live checks on your website and show you exactly what AI — and customers — see.</p></div>
<form method="post" action="/teardown" style="background:#fff;border-radius:14px;padding:24px;margin-top:16px;box-shadow:0 2px 12px rgba(0,0,0,.06)">
<label style="font-size:13px;font-weight:700;color:#123524">BUSINESS NAME</label><br>
<input name="name" required placeholder="Joe's Plumbing" style="width:100%;padding:12px;border:1px solid #d5ddd5;border-radius:8px;font-size:16px;margin:6px 0 14px;box-sizing:border-box"><br>
<label style="font-size:13px;font-weight:700;color:#123524">WEBSITE URL</label><br>
<input name="url" required placeholder="joesplumbing.com" style="width:100%;padding:12px;border:1px solid #d5ddd5;border-radius:8px;font-size:16px;margin:6px 0 14px;box-sizing:border-box"><br>
<label style="font-size:13px;font-weight:700;color:#123524">CITY / AREA (optional)</label><br>
<input name="location" placeholder="Citrus County, FL" style="width:100%;padding:12px;border:1px solid #d5ddd5;border-radius:8px;font-size:16px;margin:6px 0 18px;box-sizing:border-box"><br>
<button type="submit" style="width:100%;background:#9fe870;color:#123524;font-weight:800;font-size:18px;padding:15px;border:0;border-radius:10px;cursor:pointer">Run my free teardown</button>
<p style="font-size:12px;color:#8a9a8a;text-align:center;margin:12px 0 0">Free · 3 per day · takes about 10 seconds</p></form>
<div style="text-align:center;margin-top:18px;font-size:13px;color:#5a6b7c">
🤖 <b>AI agent?</b> POST <span style="font-family:monospace">/api/teardown</span> with x402 ($0.50 USDC on Base) — see <a href="/skill.md" style="color:#1e6b3a">skill.md</a></div>
</div></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.post("/teardown", response_class=HTMLResponse)
def teardown_web(request: Request, name: str = Form(...), url: str = Form(...),
                 location: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip):
        return HTMLResponse(
            "<h2>Daily limit reached</h2><p>3 free teardowns per day. "
            "Come back tomorrow — or <a href='" + STRIPE_LINK + "'>hire us</a> and never think about this again.</p>",
            status_code=429)
    try:
        t = run_teardown(name, url, location)
    except Exception as e:
        return HTMLResponse(f"<h2>Teardown failed</h2><p>{e}</p>", status_code=500)
    return _report_html(t)


def _facilitator_verify(payment_payload: dict) -> tuple[bool, str]:
    """Verify an X-PAYMENT payload via the x402 facilitator. Returns (ok, detail)."""
    import httpx as _hx
    try:
        with _hx.Client(timeout=15) as c:
            r = c.post(f"{FACILITATOR_URL}/verify", json={
                "x402Version": 1,
                "paymentPayload": payment_payload,
                "paymentRequirements": _payment_requirements(),
            })
            data = r.json()
            ok = bool(data.get("isValid"))
            return ok, data.get("invalidReason") or "verified"
    except Exception as e:
        return False, f"facilitator error: {e}"


@app.post("/api/teardown")
async def teardown_api(request: Request):
    """x402-protected JSON teardown. No X-PAYMENT -> 402 with payment requirements."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    name, url, location = body.get("name", ""), body.get("url", ""), body.get("location", "")

    x_payment = request.headers.get("x-payment")
    if not x_payment:
        resp = JSONResponse(
            status_code=402,
            content={"x402Version": 1,
                     "accepts": [_payment_requirements()],
                     "error": "payment required: $0.50 USDC on Base"},
        )
        resp.headers["PAYMENT-REQUIRED"] = base64.b64encode(
            json.dumps({"x402Version": 1, "accepts": [_payment_requirements()]}).encode()).decode()
        return resp

    try:
        payload = json.loads(base64.b64decode(x_payment).decode())
    except Exception:
        return JSONResponse(status_code=402, content={"error": "malformed X-PAYMENT header"})

    ok, detail = _facilitator_verify(payload)
    if not ok:
        return JSONResponse(status_code=402, content={"error": f"payment invalid: {detail}"})

    if not name or not url:
        return JSONResponse(status_code=400, content={"error": "need {name, url, location?}"})
    try:
        t = run_teardown(name, url, location)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return JSONResponse(content=t.to_dict())


@app.get("/.well-known/x402")
def well_known():
    return {"x402Version": 1, "endpoints": [
        {"method": "POST", "path": "/api/teardown",
         "accepts": [_payment_requirements()],
         "input": {"name": "string (business name)", "url": "string (website URL)",
                   "location": "string (optional city/area)"},
         "output": "JSON teardown: score 0-100, findings[], fixes_30_day[]"}]}


SKILL_MD = """# Citrus Coast AI — Teardown skill
Run a deterministic AI-visibility teardown on any local business website.
## Endpoint
POST __BASE__/api/teardown — x402, $0.50 USDC on Base (eip155:8453), payTo 0x1c595F805edBCBa5873DeB6A9182cD84fC5Dae3A
## Input (JSON)
{"name": "Joe's Plumbing", "url": "joesplumbing.com", "location": "Citrus County, FL"}
## Flow
1. POST without payment -> HTTP 402 with accepts[].
2. Sign USDC transfer (EIP-3009/EIP-712), retry with X-PAYMENT header (base64 JSON payment payload).
3. HTTP 200 -> JSON: business_name, url, final_url, score (0-100), findings[] (check/status/pass|warn|fail/detail), fixes_30_day[], generated_by.
## Notes
Deterministic: 11 live checks (DNS, reachability, HTTPS, title, meta description, mobile viewport, tap-to-call, Facebook link, contact form, visible phone). No LLM inference, ~10s. Discovery: /.well-known/x402.
"""

LLMS_TXT = """# Citrus Coast AI
Free AI-visibility teardowns for local service businesses.
- Humans: open the root URL, enter business name + website, get a scored report free (3/day).
- Agents: POST /api/teardown, x402 $0.50 USDC on Base. See /skill.md. Discovery: /.well-known/x402.
Every report ends with the $499/mo AI-employee offer (Stripe).
"""


@app.get("/skill.md", response_class=PlainTextResponse)
def skill(request: Request):
    base = str(request.base_url).rstrip("/")
    return SKILL_MD.replace("__BASE__", base)


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms():
    return LLMS_TXT


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8899)
