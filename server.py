"""
Citrus Coast AI — Model B server.
- GET  /                  web front end (free teardown form, rate-limited)
- POST /teardown          human tier: free, 3/day per IP -> HTML report + $499 CTA
- POST /api/teardown      x402 tier: $0.50 USDC on Base per call -> JSON report
- POST /api/prepay-check   x402 tier: $0.001 USDC on Base per call -> pre-pay
  legitimacy verdict for a third-party x402 endpoint (live 402 vs public listing)
- GET  /api/teardown      discovery alias: identical 402 challenge for GET-only catalogs
- GET  /api/prepay-check  discovery alias: identical 402 challenge for GET-only catalogs
- GET  /.well-known/x402  discovery for x402 indexers
- GET  /skill.md, /llms.txt  agent-readable docs

$0 to run: deterministic teardown engine (no LLM inference per call).
Payments settle to the venture Base wallet; server never holds keys.
x402 v1: verify then settle via the facilitator; the report is delivered
only after a successful settle (transaction hash returned to the buyer).
v2 migration is a separate verified step (see RECEIPTS.md) — the live
challenge stays v1 until v2 is verified end-to-end.
Referral: toll402's referral (30%, x-toll402-ref / x-toll402-agent) is
gateway-level on calls TO toll402, not a seller header on this endpoint —
no fake referral header is implemented (see RECEIPTS.md).
"""
import asyncio
import base64
import ipaddress
import json
import re
import time
from collections import defaultdict
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from teardown import run_teardown

# --- config ---
PAY_TO = "0x1c595F805edBCBa5873DeB6A9182cD84fC5Dae3A"  # venture Base wallet
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NETWORK = "eip155:8453"  # Base mainnet
PRICE_ATOMIC = "500000"  # $0.50 USDC (6 decimals)
PREPAY_PRICE_ATOMIC = "1000"  # $0.001 USDC (6 decimals) — pre-pay check price
STRIPE_LINK = "https://buy.stripe.com/3cI00l6IvdZufAUgU2eUU00"
# x402.org/facilitator is testnet-only for EVM (verified live 2026-10-16: it
# returns "No facilitator registered for scheme: exact and network: eip155:8453").
# xpay.sh is the open, no-auth mainnet facilitator; its v1 network label is "base".
FACILITATOR_URL = "https://facilitator.xpay.sh"
FACILITATOR_NETWORK = "base"  # xpay.sh v1 label for Base mainnet (402 stays CAIP-2)

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
    # Standard x402 v1 requirements. "amount" is kept for our own client's
    # backward compat; "maxAmountRequired" is the spec field facilitators read
    # (xpay.sh crashes with a BigInt error if it is missing). "extra" MUST be
    # the EIP-712 domain of the Base USDC contract — the facilitator verifies
    # the signature against it. The product description lives in the bazaar
    # extensions / well-known descriptor, not here.
    return {
        "scheme": "exact",
        "network": NETWORK,
        "asset": USDC_BASE,
        "amount": PRICE_ATOMIC,
        "maxAmountRequired": PRICE_ATOMIC,
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 300,
        "extra": {
            "name": "USD Coin",
            "version": "2",
        },
    }


def _bazaar_extensions() -> dict:
    """Bazaar/agentic-marketplace discovery metadata for the 402 response."""
    return {
        "bazaar": {
            "discoverable": True,
            "name": "AI Visibility Teardown",
            "description": ("Deterministic AI-visibility teardown for local service businesses: "
                            "11 live checks on a website (DNS, reachability, HTTPS, SEO basics, "
                            "mobile viewport, tap-to-call, Facebook link, contact form, visible phone) "
                            "with a 0-100 score, per-check findings, and 30-day fixes."),
            "input": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Business name"},
                    "url": {"type": "string", "description": "Business website URL"},
                    "location": {"type": "string", "description": "City/area (optional)"},
                },
                "required": ["name", "url"],
            },
            "output": {
                "type": "object",
                "properties": {
                    "business_name": {"type": "string"},
                    "url": {"type": "string"},
                    "final_url": {"type": "string"},
                    "score": {"type": "integer", "description": "AI-visibility score, 0-100"},
                    "findings": {"type": "array",
                                 "description": "Per-check results: check, status (pass|warn|fail), detail"},
                    "fixes_30_day": {"type": "array",
                                     "description": "Recommended fixes for the first 30 days"},
                    "generated_by": {"type": "string"},
                },
            },
        }
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


def _for_facilitator(payment_payload: dict, requirements: dict | None = None) -> tuple[dict, dict]:
    """Translate a client payment payload + our requirements into the
    facilitator's dialect. xpay.sh v1 expects network label "base" (not
    CAIP-2 eip155:8453) in BOTH the payload envelope and the requirements.
    The EIP-3009 signature is over the chainId-8453 EIP-712 domain, not over
    the network label, so relabelling does not invalidate the signature.
    The public 402 keeps CAIP-2; only the facilitator call is translated.
    `requirements` defaults to the teardown requirements; the pre-pay check
    route passes its own (same shape, $0.001 price)."""
    payload = dict(payment_payload)
    payload["network"] = FACILITATOR_NETWORK
    reqs = dict(requirements) if requirements is not None else _payment_requirements()
    reqs["network"] = FACILITATOR_NETWORK
    return payload, reqs


def _facilitator_verify(payment_payload: dict, requirements: dict | None = None) -> tuple[bool, str]:
    """Verify an X-PAYMENT payload via the x402 facilitator. Returns (ok, detail)."""
    import httpx as _hx
    payload, reqs = _for_facilitator(payment_payload, requirements)
    try:
        with _hx.Client(timeout=15) as c:
            r = c.post(f"{FACILITATOR_URL}/verify", json={
                "x402Version": 1,
                "paymentPayload": payload,
                "paymentRequirements": reqs,
            })
            data = r.json()
            ok = bool(data.get("isValid"))
            return ok, data.get("invalidReason") or "verified"
    except Exception as e:
        return False, f"facilitator error: {e}"


def _facilitator_settle(payment_payload: dict, requirements: dict | None = None) -> tuple[bool, dict, str]:
    """Settle a verified X-PAYMENT payload via the x402 facilitator.

    Returns (ok, settle_response, detail). On success settle_response holds
    {success, transaction, network, payer} — transaction is the on-chain
    settlement tx hash and the source of truth for the receipt. On failure
    nothing is delivered: the caller must return 402, never the product.
    """
    import httpx as _hx
    payload, reqs = _for_facilitator(payment_payload, requirements)
    try:
        with _hx.Client(timeout=60) as c:
            r = c.post(f"{FACILITATOR_URL}/settle", json={
                "x402Version": 1,
                "paymentPayload": payload,
                "paymentRequirements": reqs,
            })
            data = r.json()
            # Normalize the tx-hash field: xpay.sh returns txID, other
            # facilitators return transaction/txHash. Downstream reads "transaction".
            tx = data.get("transaction") or data.get("txID") or data.get("txHash")
            if bool(data.get("success")) and tx:
                data = dict(data)
                data["transaction"] = tx
                return True, data, "settled"
            reason = data.get("errorReason") or data.get("error") or "settle failed"
            return False, data, str(reason)
    except Exception as e:
        return False, {}, f"facilitator error: {e}"


SELL_RECEIPTS_PATH = "sell_receipts.jsonl"


def _log_sell_receipt(entry: dict) -> None:
    """Append one sell-side receipt (JSONL, best-effort).

    The on-chain settlement tx hash in the response is the durable receipt;
    this local file is ephemeral on Render free tier. Never raise on log
    failure — the tx hash already went back to the buyer in the response.
    """
    import os
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SELL_RECEIPTS_PATH)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _teardown_402_challenge() -> JSONResponse:
    """The identical 402 payment challenge, shared by POST (no-payment) and the
    GET discovery alias so GET-only catalogs can see the price."""
    resp = JSONResponse(
        status_code=402,
        content={"x402Version": 1,
                 "accepts": [_payment_requirements()],
                 "extensions": _bazaar_extensions(),
                 "error": "payment required: $0.50 USDC on Base"},
    )
    resp.headers["PAYMENT-REQUIRED"] = base64.b64encode(
        json.dumps({"x402Version": 1, "accepts": [_payment_requirements()]}).encode()).decode()
    return resp


@app.get("/api/teardown")
async def teardown_api_get(request: Request):
    """Discovery alias for GET-only agent catalogs: returns the identical 402
    challenge (never executes — paid execution is POST with X-PAYMENT)."""
    return _teardown_402_challenge()


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
        return _teardown_402_challenge()

    try:
        payload = json.loads(base64.b64decode(x_payment).decode())
    except Exception:
        return JSONResponse(status_code=402, content={"error": "malformed X-PAYMENT header"})

    ok, detail = _facilitator_verify(payload)
    if not ok:
        return JSONResponse(status_code=402, content={"error": f"payment invalid: {detail}"})

    # Settle BEFORE delivering the report. Without settlement the seller is
    # never paid and no settlement ever triggers Bazaar indexing — the exact
    # gap this fix closes. A failed settle means 402, never the product.
    settled, settle_resp, settle_detail = _facilitator_settle(payload)
    if not settled:
        return JSONResponse(status_code=402,
                            content={"error": f"payment settlement failed: {settle_detail}"})

    if not name or not url:
        return JSONResponse(status_code=400, content={"error": "need {name, url, location?}"})
    try:
        t = run_teardown(name, url, location)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    result = t.to_dict()
    result["settlement"] = {
        "transaction": settle_resp.get("transaction"),
        "network": settle_resp.get("network") or NETWORK,
        "payer": settle_resp.get("payer"),
    }
    _log_sell_receipt({
        "agent": "citruscoastai",
        "direction": "sell",
        "resource": "/api/teardown",
        "amount": 0.50,
        "asset": "USDC",
        "txid": settle_resp.get("transaction"),
        "network": settle_resp.get("network") or NETWORK,
        "payer": settle_resp.get("payer"),
        "pay_to": PAY_TO,
        "verdict_class": "settled",
        "http_status": 200,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00", time.gmtime()),
    })
    resp = JSONResponse(content=result)
    resp.headers["X-PAYMENT-RESPONSE"] = base64.b64encode(
        json.dumps(settle_resp, separators=(",", ":")).encode()).decode()
    return resp


# ---------------------------------------------------------------------------
# PRE-PAY CHECK — POST /api/prepay-check ($0.001 USDC on Base per call)
# Buyer sends {"url": "<endpoint URL>"}; the server fetches that endpoint's
# live 402 challenge WITHOUT paying, parses it, diffs payTo / price / asset /
# network against the endpoint's public /.well-known/x402 listing (when one
# exists), and returns a machine-readable verdict. Same x402 v1 exact-scheme
# flow as the teardown route: verify -> settle -> deliver; a failed settle
# returns 402, never the verdict. The check itself never pays the target
# endpoint and never signs anything.
# ---------------------------------------------------------------------------

def _prepay_payment_requirements() -> dict:
    """Same shape as the teardown requirements, same EIP-712 domain (Base
    USDC contract), only the price differs: $0.001 = 1000 micro-units."""
    reqs = _payment_requirements()
    reqs["amount"] = PREPAY_PRICE_ATOMIC
    reqs["maxAmountRequired"] = PREPAY_PRICE_ATOMIC
    return reqs


def _prepay_bazaar_extensions() -> dict:
    return {
        "bazaar": {
            "discoverable": True,
            "name": "x402 Pre-Pay Check",
            "description": ("Before paying an x402 endpoint, verify it: fetches the target "
                            "endpoint's live 402 challenge, diffs payTo / price / asset / "
                            "network against its public /.well-known/x402 listing, and "
                            "returns a machine-readable verdict (likely_legit / suspicious / "
                            "cannot_verify). The check itself never pays the target."),
            "input": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Endpoint URL to check"},
                },
                "required": ["url"],
            },
            "output": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "live_402_seen": {"type": "boolean"},
                    "payTo": {"type": "string"},
                    "price_usdc": {"type": "number"},
                    "host": {"type": "string"},
                    "listing_match": {"type": "string",
                                      "description": "matched | mismatched | no_listing_found | stale_suspected"},
                    "warnings": {"type": "array"},
                    "verdict": {"type": "string",
                                "description": "likely_legit | suspicious | cannot_verify"},
                    "checked_at": {"type": "string"},
                },
            },
        }
    }


def _prepay_402_challenge() -> JSONResponse:
    """The 402 payment challenge for the pre-pay check, shared by POST
    (no-payment) and the GET discovery alias."""
    resp = JSONResponse(
        status_code=402,
        content={"x402Version": 1,
                 "accepts": [_prepay_payment_requirements()],
                 "extensions": _prepay_bazaar_extensions(),
                 "error": "payment required: $0.001 USDC on Base"},
    )
    resp.headers["PAYMENT-REQUIRED"] = base64.b64encode(
        json.dumps({"x402Version": 1, "accepts": [_prepay_payment_requirements()]}).encode()).decode()
    return resp


def _check_http_client(for_host: str | None = None):
    """httpx client for fetching a target's live 402 challenge and its public
    listing. Same proxy handling as the teardown engine (trust_env=False
    avoids the httpx no_proxy IPv6 parsing bug; proxy URL from env).
    Loopback targets (127.0.0.1 / localhost) bypass the proxy: the egress
    proxy cannot CONNECT back to this VM's loopback, so a proxied self-check
    would disconnect. Public targets still go through the proxy."""
    import os
    import httpx as _hx
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    kwargs = {"timeout": 15.0, "follow_redirects": True,
              "headers": {"User-Agent": "CitrusCoastAI-PrepayCheck/1.0 (+https://citruscoast.ai)"}}
    loopback = False
    if for_host:
        if for_host == "localhost":
            loopback = True
        else:
            try:
                loopback = ipaddress.ip_address(for_host).is_loopback
            except ValueError:
                loopback = False
    if proxy and not loopback:
        kwargs.update({"trust_env": False, "proxy": proxy,
                       "verify": "/etc/ssl/certs/ca-certificates.crt"})
    else:
        kwargs.update({"trust_env": False})
    return _hx.Client(**kwargs)


def _host_blocked(host: str) -> bool:
    """SSRF guard for literal-IP / metadata targets. A host that IS an IP
    literal and is private (non-loopback), link-local, multicast, reserved,
    or unspecified is blocked — this covers the 169.254.169.254 cloud
    metadata address. DNS hostnames are NOT resolved-and-blocked here:
    on this VM public DNS resolves to the egress proxy's 198.18.0.0/15
    range, so a resolution-based block would false-positive every public
    host. Loopback is allowed (local self-checks, e.g. checking our own
    endpoint on 127.0.0.1)."""
    if not host:
        return True
    if host in ("metadata.google.internal", "metadata", "localhost"):
        # "localhost" is loopback by name; allow it below via the IP path only
        # when it is literally "localhost" — it resolves to loopback.
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # DNS hostname: fetch path applies timeouts; see note above
    if ip.is_loopback:
        return False
    return bool(ip.is_private or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified)


def _parse_402_challenge(status: int, headers, body: bytes) -> dict | None:
    """Parse an x402 v1 402 challenge (JSON body, PAYMENT-REQUIRED header as
    fallback) into the first accepts[] entry. Returns None when the response
    is not a parseable 402 challenge. This is the 402-parsing logic: the
    teardown engine has no 402 parser (it audits websites), so the parsing
    lives here and follows the same accepts[] shape the server itself emits."""
    if status != 402:
        return None
    data = {}
    try:
        data = json.loads(body.decode("utf-8", errors="ignore") or "{}")
    except Exception:
        data = {}
    accepts = []
    if isinstance(data, dict):
        accepts = data.get("accepts") or []
    if not accepts:
        hdr = ""
        try:
            hdr = headers.get("payment-required") or ""
        except Exception:
            hdr = ""
        if hdr:
            try:
                accepts = json.loads(base64.b64decode(hdr).decode()).get("accepts") or []
            except Exception:
                accepts = []
    if not accepts or not isinstance(accepts, list):
        return None
    entry = accepts[0]
    if not isinstance(entry, dict):
        return None
    raw_amount = entry.get("maxAmountRequired") or entry.get("amount")
    try:
        amount_atomic = int(str(raw_amount))
    except (TypeError, ValueError):
        return None
    return {
        "scheme": entry.get("scheme"),
        "network": entry.get("network"),
        "asset": entry.get("asset"),
        "amount_atomic": str(amount_atomic),
        "price_usdc": amount_atomic / 1_000_000,
        "payTo": entry.get("payTo"),
        "x402_version": (data.get("x402Version") if isinstance(data, dict) else None),
    }


def _normalize_target_url(target: str) -> str:
    url = (target or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _fetch_live_402(target_url: str) -> tuple[dict | None, list, str]:
    """Fetch the target endpoint's live 402 challenge WITHOUT paying.
    Tries GET (covers GET-402 endpoints like our own /api/teardown) then
    POST with an empty JSON body (covers POST-only 402 endpoints).
    Returns (parsed_challenge_or_None, warnings, detail)."""
    url = _normalize_target_url(target_url)
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return None, ["target URL has no host"], "no host in target URL"
    if _host_blocked(host):
        return None, ["target host is blocked (private / link-local / metadata address) — refusing to fetch"], "blocked host"
    warnings: list = []
    if parsed.scheme == "http":
        warnings.append("target URL uses plain HTTP; payment details could be tampered with in transit")
    last_status: int | None = None
    try:
        with _check_http_client(host) as c:
            for method in ("GET", "POST"):
                try:
                    if method == "GET":
                        r = c.get(url)
                    else:
                        r = c.post(url, json={})
                except Exception as e:
                    warnings.append(f"{method} fetch failed: {e}")
                    continue
                last_status = r.status_code
                if r.status_code != 402:
                    continue
                entry = _parse_402_challenge(r.status_code, r.headers, r.content)
                if entry is None:
                    warnings.append("endpoint returned HTTP 402 but the challenge could not be parsed (no accepts[])")
                    return None, warnings, "unparseable 402"
                final_url = str(r.url)
                final_host = urlparse(final_url).hostname or ""
                entry["host"] = final_host
                entry["requested_host"] = host
                entry["final_url"] = final_url
                if final_host != host:
                    warnings.append(f"request redirected from {host} to {final_host}; the public listing covers the requested host, not the final host")
                return entry, warnings, "live 402 seen"
        if last_status is not None:
            warnings.append(f"endpoint did not return HTTP 402 (last status {last_status})")
            return None, warnings, f"no 402 (last status {last_status})"
        return None, warnings, "fetch failed"
    except Exception as e:
        warnings.append(f"fetch failed: {e}")
        return None, warnings, "fetch error"


def _fetch_listing(target_url: str, live: dict) -> tuple[str, list]:
    """Diff the live 402 against the target origin's public /.well-known/x402
    listing. Returns (listing_match, warnings) where listing_match is one of
    matched / mismatched / no_listing_found / stale_suspected."""
    url = _normalize_target_url(target_url)
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    target_path = (parsed.path or "/").rstrip("/") or "/"
    try:
        with _check_http_client(parsed.hostname) as c:
            r = c.get(f"{origin}/.well-known/x402")
    except Exception as e:
        return "no_listing_found", [f"could not read public listing at {origin}/.well-known/x402 ({e})"]
    if r.status_code != 200:
        return "no_listing_found", []
    try:
        doc = r.json()
    except Exception:
        return "stale_suspected", ["public listing at /.well-known/x402 is not valid JSON (stale or broken listing)"]
    endpoints = doc.get("endpoints") if isinstance(doc, dict) else None
    if not isinstance(endpoints, list):
        return "stale_suspected", ["public listing at /.well-known/x402 has no endpoints[] array (stale or incomplete listing)"]
    match = None
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        ep_path = ((ep.get("path") or "").rstrip("/") or "/")
        if ep_path == target_path:
            match = ep
            break
    if match is None:
        return "no_listing_found", []
    accepts = match.get("accepts") or []
    if not accepts or not isinstance(accepts, list) or not isinstance(accepts[0], dict):
        return "stale_suspected", ["public listing entry exists for this endpoint but carries no payment terms (stale or incomplete listing)"]
    listing = accepts[0]
    diffs = []
    if (listing.get("payTo") or "").lower() != (live.get("payTo") or "").lower():
        diffs.append("payTo")
    live_amt = live.get("amount_atomic")
    list_amt = listing.get("maxAmountRequired") or listing.get("amount")
    try:
        if int(str(list_amt)) != int(str(live_amt)):
            diffs.append("price")
    except (TypeError, ValueError):
        diffs.append("price")
    if (listing.get("asset") or "").lower() != (live.get("asset") or "").lower():
        diffs.append("asset")
    if (listing.get("network") or "").lower() != (live.get("network") or "").lower():
        diffs.append("network")
    if diffs:
        return "mismatched", [f"public listing differs from the live 402 on: {', '.join(diffs)}"]
    return "matched", []


def _run_prepay_check(target_url: str) -> dict:
    """Core verdict logic (pure check — no payment, no signing). The POST
    route wraps this after verify -> settle."""
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00", time.gmtime())
    requested_host = urlparse(_normalize_target_url(target_url)).hostname or ""
    live, warnings, detail = _fetch_live_402(target_url)
    if live is None:
        warnings.append("no live 402 challenge seen — the endpoint may not be x402, may be down, or may require a specific request body")
        return {
            "url": target_url,
            "live_402_seen": False,
            "payTo": None,
            "price_usdc": None,
            "asset": None,
            "network": None,
            "host": requested_host,
            "final_url": None,
            "listing_match": "no_listing_found",
            "warnings": warnings,
            "verdict": "cannot_verify",
            "checked_at": checked_at,
        }
    listing_match, listing_warnings = _fetch_listing(target_url, live)
    warnings.extend(listing_warnings)
    pay_to = live.get("payTo")
    if not pay_to:
        warnings.append("live 402 challenge has no payTo address — do not pay it")
    if live.get("network") not in ("eip155:8453", "base"):
        warnings.append(f"live 402 network is {live.get('network')}, not Base mainnet (eip155:8453)")
    if (live.get("asset") or "").lower() != USDC_BASE.lower():
        warnings.append("live 402 asset is not Base USDC (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913)")
    if live.get("price_usdc") == 0:
        warnings.append("live 402 price is zero — unusual for a paid endpoint")
    redirected = bool(live.get("host") and live.get("requested_host")
                      and live["host"] != live["requested_host"])
    if redirected:
        verdict = "suspicious"
    elif not pay_to:
        verdict = "suspicious"
    elif listing_match == "matched":
        verdict = "likely_legit"
    elif listing_match == "mismatched":
        verdict = "suspicious"
    elif listing_match == "stale_suspected":
        verdict = "cannot_verify"
    else:  # no_listing_found
        warnings.append("no public listing found for this endpoint; a live 402 alone does not prove legitimacy")
        verdict = "cannot_verify"
    return {
        "url": target_url,
        "live_402_seen": True,
        "payTo": pay_to,
        "price_usdc": live.get("price_usdc"),
        "asset": live.get("asset"),
        "network": live.get("network"),
        "host": live.get("host"),
        "final_url": live.get("final_url"),
        "listing_match": listing_match,
        "warnings": warnings,
        "verdict": verdict,
        "checked_at": checked_at,
    }


@app.get("/api/prepay-check")
async def prepay_check_get(request: Request):
    """Discovery alias for GET-only agent catalogs: returns the identical 402
    challenge (never executes — paid execution is POST with X-PAYMENT)."""
    return _prepay_402_challenge()


@app.post("/api/prepay-check")
async def prepay_check(request: Request):
    """x402-protected pre-pay check. No X-PAYMENT -> 402 with payment requirements."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = (body.get("url") or "").strip()

    x_payment = request.headers.get("x-payment")
    if not x_payment:
        return _prepay_402_challenge()

    try:
        payload = json.loads(base64.b64decode(x_payment).decode())
    except Exception:
        return JSONResponse(status_code=402, content={"error": "malformed X-PAYMENT header"})

    reqs = _prepay_payment_requirements()
    ok, detail = _facilitator_verify(payload, reqs)
    if not ok:
        return JSONResponse(status_code=402, content={"error": f"payment invalid: {detail}"})

    # Settle BEFORE delivering the verdict. A failed settle means 402, never
    # the verdict — same rule as the teardown route.
    settled, settle_resp, settle_detail = _facilitator_settle(payload, reqs)
    if not settled:
        return JSONResponse(status_code=402,
                            content={"error": f"payment settlement failed: {settle_detail}"})

    if not target:
        return JSONResponse(status_code=400, content={"error": "need {url}"})
    try:
        # Run the blocking sync fetch in a worker thread: _run_prepay_check uses
        # sync httpx, and calling it inline would block the event loop — a
        # self-target fetch (e.g. checking our own endpoint) would then
        # deadlock until the fetch timeouts fire. to_thread keeps the loop
        # free to serve the self-fetch.
        result = await asyncio.to_thread(_run_prepay_check, target)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    result["settlement"] = {
        "transaction": settle_resp.get("transaction"),
        "network": settle_resp.get("network") or NETWORK,
        "payer": settle_resp.get("payer"),
    }
    _log_sell_receipt({
        "agent": "citruscoastai",
        "direction": "sell",
        "resource": "/api/prepay-check",
        "amount": 0.001,
        "asset": "USDC",
        "txid": settle_resp.get("transaction"),
        "network": settle_resp.get("network") or NETWORK,
        "payer": settle_resp.get("payer"),
        "pay_to": PAY_TO,
        "verdict_class": "settled",
        "http_status": 200,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00", time.gmtime()),
    })
    resp = JSONResponse(content=result)
    resp.headers["X-PAYMENT-RESPONSE"] = base64.b64encode(
        json.dumps(settle_resp, separators=(",", ":")).encode()).decode()
    return resp


def _agent_card(base: str) -> dict:
    """Google A2A protocol v1.0 Agent Card."""
    return {
        "name": "Citrus Coast AI Teardown",
        "description": ("Deterministic AI-visibility teardown for local service businesses: "
                        "11 live checks on a website (DNS, reachability, HTTPS, SEO basics, "
                        "mobile viewport, tap-to-call, Facebook link, contact form, visible phone) "
                        "with a 0-100 score, per-check findings, and 30-day fixes. "
                        "Free for humans via web form (3/day); $0.50 USDC on Base per API call via x402."),
        "url": base,
        "version": "1.0.0",
        "provider": {
            "name": "Citrus Coast AI",
            "url": "https://github.com/mykel1283nelson-hub/citrus-coast-ai-teardown",
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text", "application/json"],
        "skills": [
            {
                "id": "ai-visibility-teardown",
                "name": "AI Visibility Teardown",
                "description": ("Run a deterministic AI-visibility teardown on any business website. "
                                "Returns a 0-100 score, per-check findings, and 30-day fixes as JSON."),
                "tags": ["seo", "audit", "small-business", "x402"],
                "examples": [
                    "Run a teardown on joesplumbing.com",
                    "Audit the AI visibility of a local business website",
                ],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "x402-paid-teardown",
                "name": "x402 Paid Teardown Endpoint",
                "description": ("Programmatic access to the teardown engine via the x402 payment protocol: "
                                "POST /api/teardown returns HTTP 402 with payment requirements "
                                "($0.50 USDC on Base, eip155:8453); retry with an X-PAYMENT header "
                                "containing the signed payment payload for a 200 JSON report."),
                "tags": ["x402", "api", "payments", "base"],
                "examples": [
                    "Call the teardown API with x402 payment",
                ],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "x402-prepay-check",
                "name": "x402 Pre-Pay Check",
                "description": ("Before paying an x402 endpoint, verify it: POST /api/prepay-check "
                                "with {\"url\": \"<endpoint URL>\"} returns HTTP 402 with payment "
                                "requirements ($0.001 USDC on Base, eip155:8453); retry with an "
                                "X-PAYMENT header for a 200 verdict (likely_legit / suspicious / "
                                "cannot_verify) comparing the target's live 402 against its public "
                                "/.well-known/x402 listing. The check itself never pays the target."),
                "tags": ["x402", "api", "payments", "base", "verification"],
                "examples": [
                    "Check an x402 endpoint before paying it",
                ],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
        ],
    }


@app.get("/.well-known/agent.json")
def agent_card(request: Request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse(content=_agent_card(base))


@app.get("/.well-known/x402")
def well_known():
    return {"x402Version": 1, "endpoints": [
        {"method": "POST", "path": "/api/teardown",
         "accepts": [_payment_requirements()],
         "input": {"name": "string (business name)", "url": "string (website URL)",
                   "location": "string (optional city/area)"},
         "output": "JSON teardown: score 0-100, findings[], fixes_30_day[]"},
        {"method": "GET", "path": "/api/teardown",
         "note": "discovery only: returns the identical 402 payment challenge so GET-only "
                 "catalogs can see the price; paid execution is POST with X-PAYMENT"},
        {"method": "POST", "path": "/api/prepay-check",
         "accepts": [_prepay_payment_requirements()],
         "input": {"url": "string (x402 endpoint URL to check before paying)"},
         "output": "JSON verdict: url, live_402_seen, payTo, price_usdc, host, listing_match, warnings[], verdict (likely_legit|suspicious|cannot_verify), checked_at"},
        {"method": "GET", "path": "/api/prepay-check",
         "note": "discovery only: returns the identical 402 payment challenge so GET-only "
                 "catalogs can see the price; paid execution is POST with X-PAYMENT"}]}


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

## Pre-pay check (verify an x402 endpoint BEFORE paying it)
POST __BASE__/api/prepay-check — x402, $0.001 USDC on Base (eip155:8453), payTo 0x1c595F805edBCBa5873DeB6A9182cD84fC5Dae3A
## Input (JSON)
{"url": "https://example.com/api/paid-endpoint"}
## Flow
1. POST without payment -> HTTP 402 with accepts[].
2. Sign USDC transfer (EIP-3009/EIP-712), retry with X-PAYMENT header (base64 JSON payment payload).
3. HTTP 200 -> JSON verdict: url, live_402_seen, payTo, price_usdc, host, listing_match (matched|mismatched|no_listing_found|stale_suspected), warnings[], verdict (likely_legit|suspicious|cannot_verify), checked_at.
## Notes
The check fetches the target's live 402 challenge WITHOUT paying it and diffs payTo / price / asset / network against the target origin's public /.well-known/x402 listing. A live 402 alone does not prove legitimacy — no_listing_found yields cannot_verify. Discovery: /.well-known/x402.
"""

LLMS_TXT = """# Citrus Coast AI
Free AI-visibility teardowns for local service businesses.
- Humans: open the root URL, enter business name + website, get a scored report free (3/day).
- Agents: POST /api/teardown, x402 $0.50 USDC on Base. See /skill.md. Discovery: /.well-known/x402.
- Agents: POST /api/prepay-check, x402 $0.001 USDC on Base — verdict on an x402 endpoint BEFORE paying it (live 402 vs public listing). See /skill.md.
Every report ends with the $499/mo AI-employee offer (Stripe).
"""


VERIFY_HASH_402INDEX = "5fa074deec27e4bbf42d0b89b04bfcd4e2c85258a2e9ec7bbe8a7c830f2c2c8e"


@app.get("/.well-known/402index-verify.txt", response_class=PlainTextResponse)
def verify_402index():
    return VERIFY_HASH_402INDEX


@app.get("/skill.md", response_class=PlainTextResponse)
def skill(request: Request):
    base = str(request.base_url).rstrip("/")
    return SKILL_MD.replace("__BASE__", base)


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms():
    return LLMS_TXT


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
