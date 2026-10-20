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
- GET  /ledger.json  public machine-readable ledger (balances + receipt rows)

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
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from teardown import run_teardown
from trust_teardown import run_trust_teardown

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

# --- canonical ledger (public, machine-readable) ---
# Served at GET /ledger.json — the live read path promised to clawdsmith (Oct 10).
# ledger.json is copied verbatim from ai-employee-agency/hidden_files/ledger.json
# at push time. Contents are public by design: balances + receipt rows (public
# wallet addresses + txids). No secrets or PII — verified by reading the file
# before copying (no keys, passwords, tokens, names, or personal identifiers).
_LEDGER_PATH = Path(__file__).resolve().parent / "ledger.json"
try:
    _LEDGER_DATA = json.loads(_LEDGER_PATH.read_text(encoding="utf-8"))
except Exception:
    _LEDGER_DATA = None

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


def _trust_payment_requirements() -> dict:
    """Same $0.50 shape as the old teardown requirements — the paid tier of
    the Agent Trust Teardown (full per-check evidence) costs $0.50 USDC."""
    return _payment_requirements()


def _trust_bazaar_extensions() -> dict:
    """Bazaar/agentic-marketplace discovery metadata for the reframed
    Agent Trust Teardown (2026-10-18): a deep trust report on an x402 agent
    endpoint, not a website audit."""
    return {
        "bazaar": {
            "discoverable": True,
            "name": "Agent Trust Teardown",
            "description": ("Deep trust report on an x402 agent endpoint: liveness probe, "
                            "live-402 terms audit (payTo / price / asset / network), discovery-doc "
                            "agreement, payTo history, and red-flag aggregation into a 0-100 trust "
                            "score with a TRUST / CAUTION / DO NOT PAY verdict. Delivery-first: "
                            "the verdict + score are free; the full per-check evidence is $0.50 "
                            "USDC on Base via x402."),
            "input": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "x402 endpoint URL to tear down"},
                    "claimed": {
                        "type": "object",
                        "description": "Optional claimed terms to audit the live 402 against "
                                       "(payTo, price_usdc, asset, network)",
                        "properties": {
                            "payTo": {"type": "string"},
                            "price_usdc": {"type": "number"},
                            "asset": {"type": "string"},
                            "network": {"type": "string"},
                        },
                    },
                },
                "required": ["url"],
            },
            "output": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "description": "TRUST | CAUTION | DO NOT PAY"},
                    "trust_score": {"type": "integer", "description": "0-100 trust score"},
                    "checks": {"type": "array",
                               "description": "Per-check results: check, status (pass|warn|fail), detail"},
                    "evidence": {"type": "object",
                                 "description": "Full per-check evidence (paid tier only)"},
                    "checked_at": {"type": "string"},
                },
            },
        }
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
<title>Agent Trust Teardown — Citrus Coast AI</title></head>
<body style="font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;margin:0;background:#0f1419;color:#e8ecef">
<div style="max-width:600px;margin:0 auto;padding:24px">
<div style="background:linear-gradient(160deg,#123524,#1e6b3a);color:#fff;border-radius:14px;padding:34px 26px;text-align:center">
<div style="font-size:12px;letter-spacing:2px;opacity:.8">CITRUS COAST AI — AGENT TRUST PLATFORM</div>
<h1 style="margin:10px 0">Verify before you pay.</h1>
<p style="opacity:.88;font-size:15px;line-height:1.55;margin:0">Point us at any x402 agent endpoint. We probe it live, audit its payment terms, check its discovery docs, and give you a TRUST / CAUTION / DO NOT PAY verdict with a 0-100 score. The verdict is free — always.</p></div>
<form method="post" action="/teardown" style="background:#1a222b;border-radius:14px;padding:24px;margin-top:16px;box-shadow:0 2px 12px rgba(0,0,0,.3)">
<label style="font-size:13px;font-weight:700;color:#9fe870">X402 ENDPOINT URL</label><br>
<input name="url" required placeholder="https://example.com/api/paid-endpoint" style="width:100%;padding:12px;border:1px solid #2a3542;border-radius:8px;font-size:16px;margin:6px 0 18px;box-sizing:border-box;background:#0f1419;color:#e8ecef"><br>
<button type="submit" style="width:100%;background:#9fe870;color:#123524;font-weight:800;font-size:18px;padding:15px;border:0;border-radius:10px;cursor:pointer">Run my free trust teardown</button>
<p style="font-size:12px;color:#8a9a8a;text-align:center;margin:12px 0 0">Free · verdict + score · full evidence $0.50 via x402</p></form>
<div style="text-align:center;margin-top:18px;font-size:13px;color:#8a9a8a">
🤖 <b>AI agent?</b> POST <span style="font-family:monospace">/api/teardown</span> {"url": "..."} — verdict free, full evidence $0.50 USDC on Base — see <a href="/skill.md" style="color:#9fe870">skill.md</a></div>
</div></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.post("/teardown", response_class=HTMLResponse)
def teardown_web(request: Request, url: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip):
        return HTMLResponse(
            "<h2>Daily limit reached</h2><p>3 free trust teardowns per day. "
            "Come back tomorrow.</p>",
            status_code=429)
    try:
        report = run_trust_teardown(url)
    except Exception as e:
        return HTMLResponse(f"<h2>Teardown failed</h2><p>{e}</p>", status_code=500)
    return _trust_report_html(report)


def _trust_report_html(r) -> str:
    color = {"TRUST": "#1e6b3a", "CAUTION": "#b7791f", "DO NOT PAY": "#b3261e"}
    icon = {"pass": "✓", "warn": "!", "fail": "✕"}
    ccolor = {"pass": "#1e6b3a", "warn": "#b7791f", "fail": "#b3261e"}
    rows = ""
    for f in r.checks:
        rows += (f"<div style='border-left:4px solid {ccolor[f['status']]};padding:10px 12px;margin:8px 0;"
                 f"background:#1a222b;'><b>{icon[f['status']]} {f['check']}</b><br>"
                 f"<span style='color:#aeb9c2;font-size:14px'>{f['detail']}</span></div>")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trust Teardown: {r.requested_url} — Citrus Coast AI</title></head>
<body style="font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;margin:0;background:#0f1419;color:#e8ecef">
<div style="max-width:640px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(160deg,#123524,#1e6b3a);color:#fff;border-radius:14px;padding:28px 24px;text-align:center">
<div style="font-size:12px;letter-spacing:2px;opacity:.8">CITRUS COAST AI — TRUST TEARDOWN</div>
<div style="font-size:13px;opacity:.85;word-break:break-all;margin-top:8px">{r.url}</div>
<div style="font-size:44px;font-weight:800;margin:12px 0 0;color:{color[r.verdict]};background:#fff;border-radius:10px;padding:8px">{r.verdict}</div>
<div style="font-size:52px;font-weight:800;margin:8px 0 0;color:#9fe870">{r.score}<span style="font-size:20px">/100</span></div>
<div style="font-size:13px;opacity:.85">trust score</div></div>
<h2 style="color:#9fe870;margin:22px 0 6px">What we checked</h2>{rows}
<div style="background:#1a222b;border-radius:14px;padding:26px 24px;text-align:center;margin-top:24px">
<h2 style="margin:0 0 8px;color:#fff">Want the full evidence?</h2>
<p style="color:#aeb9c2;font-size:15px;margin:0 0 18px">Raw 402 challenge, probe timings, redirect chain, discovery doc contents, terms diff, payTo history — $0.50 USDC on Base via x402.</p>
<p style="font-family:monospace;font-size:13px;color:#9fe870;word-break:break-all">POST /api/teardown {{"url": "{r.requested_url}"}} + X-PAYMENT</p></div>
<p style="font-size:11px;color:#5a6b7c;text-align:center;margin-top:18px">Deterministic audit — 6 weighted checks, no guesswork. Checked {r.checked_at}.</p>
</div></body></html>"""


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
    """The 402 payment challenge for the Agent Trust Teardown's paid tier
    (full per-check evidence, $0.50). Shared by the GET discovery alias.
    The free tier (verdict + score) needs no payment — POST without X-PAYMENT
    returns it as HTTP 200."""
    resp = JSONResponse(
        status_code=402,
        content={"x402Version": 1,
                 "accepts": [_trust_payment_requirements()],
                 "extensions": _trust_bazaar_extensions(),
                 "error": "payment required: $0.50 USDC on Base for the full trust-teardown evidence report (verdict + score are free via POST without payment)"},
    )
    resp.headers["PAYMENT-REQUIRED"] = base64.b64encode(
        json.dumps({"x402Version": 1, "accepts": [_trust_payment_requirements()]}).encode()).decode()
    return resp


@app.get("/api/teardown")
async def teardown_api_get(request: Request):
    """Discovery alias for GET-only agent catalogs: returns the identical 402
    challenge (never executes — paid execution is POST with X-PAYMENT)."""
    return _teardown_402_challenge()


@app.post("/api/teardown")
async def teardown_api(request: Request):
    """Agent Trust Teardown (reframed 2026-10-18): deep trust report on an
    x402 agent endpoint. DELIVERY-FIRST: POST {"url": ...} with NO payment
    returns the verdict + score + check summaries free (HTTP 200). POST the
    same body WITH an X-PAYMENT header ($0.50 USDC on Base via x402) returns
    the full per-check evidence after verify -> settle."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = (body.get("url") or "").strip()
    claimed = body.get("claimed") or {}
    if not isinstance(claimed, dict):
        claimed = {}

    x_payment = request.headers.get("x-payment")
    if not x_payment:
        # FREE TIER — verdict + score, no payment required.
        if not url:
            return JSONResponse(status_code=400, content={
                "error": "need {url, claimed?}",
                "free_tier": "POST {\"url\": \"<x402 endpoint URL>\"} returns verdict + score free",
                "paid_tier": "same request with X-PAYMENT header ($0.50 USDC on Base) returns full evidence",
            })
        try:
            report = await asyncio.to_thread(run_trust_teardown, url, claimed)
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})
        summary = report.free_summary()
        summary["payment_requirements"] = _trust_payment_requirements()
        return JSONResponse(content=summary)

    # PAID TIER — verify -> settle -> full evidence.
    try:
        payload = json.loads(base64.b64decode(x_payment).decode())
    except Exception:
        return JSONResponse(status_code=402, content={"error": "malformed X-PAYMENT header"})

    ok, detail = _facilitator_verify(payload)
    if not ok:
        return JSONResponse(status_code=402, content={"error": f"payment invalid: {detail}"})

    # Settle BEFORE delivering the evidence. A failed settle means 402,
    # never the product.
    settled, settle_resp, settle_detail = _facilitator_settle(payload)
    if not settled:
        return JSONResponse(status_code=402,
                            content={"error": f"payment settlement failed: {settle_detail}"})

    if not url:
        return JSONResponse(status_code=400, content={"error": "need {url, claimed?}"})
    try:
        report = await asyncio.to_thread(run_trust_teardown, url, claimed)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    result = report.full_report()
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
        "name": "Citrus Coast AI — Agent Trust Teardown",
        "description": ("Deep trust report on an x402 agent endpoint: liveness probe, live-402 "
                        "terms audit (payTo / price / asset / network), discovery-doc agreement, "
                        "payTo history, and red-flag aggregation into a 0-100 trust score with a "
                        "TRUST / CAUTION / DO NOT PAY verdict. Delivery-first: verdict + score are "
                        "free; full per-check evidence is $0.50 USDC on Base via x402."),
        "url": base,
        "version": "2.0.0",
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
                "id": "agent-trust-teardown",
                "name": "Agent Trust Teardown",
                "description": ("Deep trust report on any x402 agent endpoint. POST {\"url\": ...} "
                                "free -> verdict (TRUST/CAUTION/DO NOT PAY) + 0-100 trust score + "
                                "check summaries. Same request with X-PAYMENT ($0.50 USDC on Base) "
                                "-> full per-check evidence."),
                "tags": ["x402", "trust", "audit", "verification", "base"],
                "examples": [
                    "Tear down this x402 endpoint before I pay it",
                    "Is this agent endpoint trustworthy?",
                ],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "x402-paid-trust-evidence",
                "name": "x402 Paid Trust Evidence",
                "description": ("Programmatic access to full trust-teardown evidence via the x402 "
                                "payment protocol: POST /api/teardown returns the free verdict; "
                                "retry with an X-PAYMENT header containing the signed payment "
                                "payload ($0.50 USDC on Base, eip155:8453) for the 200 JSON evidence "
                                "report."),
                "tags": ["x402", "api", "payments", "base"],
                "examples": [
                    "Get the full evidence report with x402 payment",
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
         "accepts": [_trust_payment_requirements()],
         "free_tier": "verdict + trust score + check summaries, no payment (HTTP 200)",
         "paid_tier": "full per-check evidence, $0.50 USDC on Base via X-PAYMENT header",
         "input": {"url": "string (x402 endpoint URL to tear down)",
                   "claimed": "object (optional claimed terms to audit: payTo, price_usdc, asset, network)"},
         "output": "JSON trust report: verdict (TRUST|CAUTION|DO NOT PAY), trust_score 0-100, checks[], evidence{} (paid tier)"},
        {"method": "GET", "path": "/api/teardown",
         "note": "discovery only: returns the 402 payment challenge for the paid tier so GET-only "
                 "catalogs can see the price; the free tier (verdict + score) is POST without X-PAYMENT"},
        {"method": "POST", "path": "/api/prepay-check",
         "accepts": [_prepay_payment_requirements()],
         "input": {"url": "string (x402 endpoint URL to check before paying)"},
         "output": "JSON verdict: url, live_402_seen, payTo, price_usdc, host, listing_match, warnings[], verdict (likely_legit|suspicious|cannot_verify), checked_at"},
        {"method": "GET", "path": "/api/prepay-check",
         "note": "discovery only: returns the identical 402 payment challenge so GET-only "
                 "catalogs can see the price; paid execution is POST with X-PAYMENT"}]}


SKILL_MD = """# Citrus Coast AI — Agent Trust Teardown skill
Deep trust report on any x402 agent endpoint. Delivery-first: the verdict + score are free.
## Endpoint
POST __BASE__/api/teardown — free tier: {"url": "<x402 endpoint URL>"} -> HTTP 200 verdict + score.
Paid tier: same request with X-PAYMENT header ($0.50 USDC on Base, eip155:8453, payTo 0x1c595F805edBCBa5873DeB6A9182cD84fC5Dae3A) -> HTTP 200 full per-check evidence.
## Input (JSON)
{"url": "https://example.com/api/paid-endpoint", "claimed": {"payTo": "0x...", "price_usdc": 1.0}}
The optional "claimed" object holds terms you believe the endpoint advertises — the teardown diffs the live 402 against them.
## Free tier (no payment)
POST {"url": "..."} -> HTTP 200: service, url, verdict (TRUST | CAUTION | DO NOT PAY), trust_score (0-100), checks[] (check/status/detail), checked_at, plus payment_requirements for the paid tier.
## Paid tier
1. Read payment_requirements from the free response (or GET /api/teardown -> HTTP 402 challenge).
2. Sign USDC transfer (EIP-3009/EIP-712), retry with X-PAYMENT header (base64 JSON payment payload).
3. HTTP 200 -> full report: everything in the free tier + evidence{} (raw 402, probe timings, redirect chain, discovery doc contents, terms diff, payTo history).
## Notes
Deterministic: 6 weighted checks (liveness, challenge parse, terms audit, discovery docs, identity/history, red flags). No LLM inference. The check never pays the target endpoint and never signs anything. Discovery: /.well-known/x402.

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

LLMS_TXT = """# Citrus Coast AI — Agent Trust Teardown
Deep trust report on any x402 agent endpoint. Delivery-first: verdict + score free.
- Agents: POST /api/teardown {"url": "<x402 endpoint>"} -> free verdict (TRUST/CAUTION/DO NOT PAY) + 0-100 score. Same request with X-PAYMENT ($0.50 USDC on Base) -> full per-check evidence. See /skill.md. Discovery: /.well-known/x402.
- Agents: POST /api/prepay-check, x402 $0.001 USDC on Base — quick verdict on an x402 endpoint BEFORE paying it (live 402 vs public listing). See /skill.md.
- Humans: free trust teardown via the web form (3/day).
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


@app.get("/ledger.json")
def ledger():
    if _LEDGER_DATA is None:
        return JSONResponse(content={"error": "ledger unavailable"}, status_code=503)
    return JSONResponse(content=_LEDGER_DATA)


# ---------------------------------------------------------------------------
# NEAR AI AGENT MARKET — self-hosted hire receiver (runtime="http" agents)
# Added 2026-10-20. Our five per-call listings on market.near.ai point their
# webhook_url at POST /near/<service> on this host. Contract per the market's
# own docs (/skill/reference/webhooks.md + /skill/reference/agent-api.md):
#   - Deliveries are HMAC-signed: X-Market-Signature is
#     "sha256=" + HMAC-SHA256(webhook_secret, "<unix-ts>.<raw body>"), with
#     the timestamp in X-Market-Timestamp, the event in X-Market-Event, and
#     a dedupe id in X-Market-Delivery.
#   - We ack 2xx fast (a 2xx on hire.created also stamps the assignment's
#     started_at), then fulfill asynchronously and deliver via the worker
#     API: POST {market}/v1/assignments/{id}/submit {"deliverableMarkdown"}
#     with the listing's own aat_ token as a Bearer credential.
#   - Secrets/tokens come from env only (NEAR_WEBHOOK_SECRET_<SVC>,
#     NEAR_API_TOKEN_<SVC>) — never from this repo. A route whose secret is
#     not configured fails closed (503) so a misconfigured deploy can never
#     accept unsigned work.
#   - The hire payload's mcp.session_token is a secret: it is NEVER logged.
#     Only whitelisted metadata fields reach the hires log.
# Fulfillment level per service (v1):
#   extract   REAL — fetches the brief's URL (SSRF-guarded via _host_blocked)
#             and returns trafilatura-cleaned text.
#   verify    REAL — runs run_trust_teardown() on the brief's URL (the same
#             engine as /api/teardown's free tier) and returns the verdict.
#   pr-review REAL when the brief names a GitHub PR URL — live PR metadata,
#             diff stats and risk flags via the public GitHub API; otherwise
#             a structured review checklist for the brief.
#   research  FIRST-PASS — a structured brief generated from the request
#             text (questions decomposed, source leads, method; a URL named
#             in the brief is fetched and excerpted). Labeled first-pass in
#             the deliverable itself.
#   datasets  FIRST-PASS — dataset-catalog leads matched to the brief's
#             keywords (constructed search links) + an evaluation checklist.
#             Labeled first-pass in the deliverable itself.
# TODO(v2): reconciliation poll of GET /v1/agents/me/assignments (the market
# documents that a hire.created delivery can be dropped at queue time), and
# file deliverables via POST /v1/files when a result outgrows Markdown.
# ---------------------------------------------------------------------------
import hashlib
import hmac
import os
from urllib.parse import quote_plus

try:
    import httpx
except Exception:  # requirements pin httpx; guard so a broken env can't
    httpx = None   # take the whole server down — fulfillment logs instead.

NEAR_MARKET_BASE = os.environ.get(
    "NEAR_MARKET_BASE", "https://market.near.ai").rstrip("/")
NEAR_SERVICES = ("verify", "research", "datasets", "pr-review", "extract")
NEAR_HIRES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "near_hires.jsonl")
NEAR_DELIVERABLE_CAP = 240_000  # market submit cap: 256 KiB of Markdown
_near_seen_deliveries = set()  # in-memory dedupe; a platform-side identical
                               # resubmit is a documented no-op, so a lost
                               # set on restart costs at most a rebuild.


def _near_env(service: str, kind: str) -> str:
    suffix = service.upper().replace("-", "_")
    return os.environ.get(f"NEAR_{kind}_{suffix}", "").strip()


def _near_log(entry: dict) -> None:
    """Append one hire-receiver record (JSONL, best-effort — mirrors
    _log_sell_receipt; ephemeral on Render free tier; never raises)."""
    try:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  **entry}
        with open(NEAR_HIRES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _near_signature_ok(service: str, headers, raw_body: bytes) -> bool:
    whsec = _near_env(service, "WEBHOOK" + "_SECRET")
    if not whsec:
        return False
    sig = headers.get("x-market-signature") or ""
    ts = headers.get("x-market-timestamp") or ""
    if not ts or not sig.startswith("sha256="):
        return False
    expected = hmac.new(whsec.encode(), ts.encode() + b"." + raw_body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig[len("sha256="):])


def _near_brief(payload: dict) -> str:
    return ((payload.get("title") or "") + "\n"
            + (payload.get("description") or "")).strip()


def _near_find_urls(text: str) -> list:
    return re.findall(r"https?://[^\s<>\"')\]]+", text or "")


def _near_fetch_clean(url: str) -> tuple:
    """Fetch a public URL -> (page_title, cleaned_text). Raises ValueError
    with a buyer-safe message on any refusal or failure."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("not a public http(s) URL")
    if _host_blocked(parsed.hostname):
        raise ValueError("host is blocked (private/metadata address)")
    client = _check_http_client(parsed.hostname)
    resp = client.get(url, timeout=25)
    if resp.status_code >= 400:
        raise ValueError(f"fetch failed: HTTP {resp.status_code}")
    html = resp.text or ""
    text = ""
    try:
        import trafilatura
        text = trafilatura.extract(html) or ""
    except Exception:
        text = ""
    if not text:
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>",
                      " ", html)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    title = ""
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title, text


def _near_deliver_extract(payload: dict) -> str:
    brief = _near_brief(payload)
    urls = _near_find_urls(brief)
    if not urls:
        return ("# Web Page Extraction\n\nNo URL was found in the hire "
                "brief, so nothing could be extracted. Resubmit with the "
                "page URL in the title or description and the cleaned text "
                "is returned on the next call.\n")
    url = urls[0]
    try:
        title, text = _near_fetch_clean(url)
    except Exception as e:
        return (f"# Web Page Extraction — fetch failed\n\n- Source: {url}\n"
                f"- Result: could not retrieve the page ({e}). No content "
                "is fabricated in its place.\n")
    if not text:
        return (f"# Web Page Extraction — no readable text\n\n- Source: "
                f"{url}\n- The page was fetched but yielded no extractable "
                "text (it may be script-rendered or paywalled).\n")
    return (f"# Extracted text — {title or url}\n\n- Source: {url}\n"
            f"- Extracted by: Citrus Coast AI (trafilatura clean-text)\n\n"
            f"---\n\n{text}\n")


def _near_deliver_verify(payload: dict) -> str:
    brief = _near_brief(payload)
    urls = _near_find_urls(brief)
    addrs = re.findall(r"0x[0-9a-fA-F]{40}", brief)
    agent_ids = re.findall(r"(?i)agent\s*id[:#]?\s*(\d{1,10})", brief)
    head = "# Agent Verification Pass — Citrus Coast AI\n"
    notes = []
    if addrs:
        notes.append(f"- Wallet/contract address named in brief: {addrs[0]}")
    if agent_ids:
        notes.append(f"- ERC-8004 agent id named in brief: {agent_ids[0]}")
    if not urls:
        return (head + "\n".join(notes) + "\n\nNo endpoint URL was found "
                "in the hire brief, so the live trust teardown could not "
                "run. The pass executes against the agent's x402 endpoint "
                "URL: resubmit with the URL in the title or description "
                "and the verdict, 0-100 trust score, and per-check results "
                "are returned on the next call.\n")
    target = urls[0]
    try:
        report = run_trust_teardown(target, {})
        s = report.free_summary()
    except Exception as e:
        return (head + "\n".join(notes) +
                f"\n\n- Target: {target}\n- Result: the teardown engine "
                f"could not complete against the live endpoint ({e}). "
                "No verdict is issued rather than a guessed one.\n")
    lines = [head, f"- Target: {target}",
             f"- **Verdict: {s.get('verdict')}**",
             f"- **Trust score: {s.get('trust_score')}/100**",
             f"- Checked at: {s.get('checked_at')} "
             f"({s.get('elapsed_ms')} ms)"]
    lines += notes
    lines.append("\n## Checks\n")
    for c in s.get("checks", []):
        lines.append(f"- **{c.get('check')}** — {c.get('status')}: "
                     f"{c.get('detail')}")
    lines.append("\nFull per-check evidence (raw 402 challenge, probe "
                 "timings, discovery-doc diff, payTo history) is the "
                 "$0.50 tier of this same engine via x402 at "
                 "citrus-coast-ai-teardown.onrender.com/api/teardown.\n")
    return "\n".join(lines) + "\n"


def _near_deliver_pr_review(payload: dict) -> str:
    brief = _near_brief(payload)
    m = re.search(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", brief)
    if not m:
        return ("# GitHub PR Review — no PR URL in brief\n\nNo "
                "github.com/<owner>/<repo>/pull/<n> URL was found in the "
                "hire brief, so no live review could run. Resubmit with "
                "the PR URL and the next call returns live metadata, diff "
                "stats, and risk flags. Review checklist applied to any "
                "PR: scope vs. description, correctness of the diff, "
                "security-sensitive paths (workflows, secrets, auth), "
                "test coverage of changed code, and change size.\n")
    owner, repo, num = m.group(1), m.group(2), m.group(3)
    api = f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}"
    try:
        client = _check_http_client("api.github.com")
        resp = client.get(api, timeout=25, headers={
            "Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            raise ValueError(f"GitHub API HTTP {resp.status_code}")
        pr = resp.json()
        files = []
        try:
            fr = client.get(api + "/files", timeout=25, headers={
                "Accept": "application/vnd.github+json"},
                params={"per_page": 100})
            if fr.status_code == 200:
                files = fr.json()
        except Exception:
            files = []
    except Exception as e:
        return (f"# GitHub PR Review — lookup failed\n\n- PR: "
                f"https://github.com/{owner}/{repo}/pull/{num}\n- Result: "
                f"the public GitHub API could not be read ({e}). No "
                "review is fabricated in its place.\n")
    flags = []
    names = [f.get("filename", "") for f in files if isinstance(f, dict)]
    if any(".github/workflows/" in n for n in names):
        flags.append("touches CI workflow files (.github/workflows) — "
                     "review for action-pinning and permission changes")
    if any(re.search(r"(?i)(secret|credential|\.env|token|private[_-]?key)",
                     n) for n in names):
        flags.append("touches a sensitive-looking path (secret/credential/"
                     "env/key) — verify no credential material is added")
    code_changed = any(n.rsplit(".", 1)[-1] in
                       ("py", "js", "ts", "tsx", "jsx", "go", "rs", "java",
                        "rb", "php", "c", "cpp", "cs", "sh")
                       for n in names)
    if code_changed and not any("test" in n.lower() for n in names):
        flags.append("code changed but no test files changed — ask for "
                     "test coverage of the new behavior")
    if (pr.get("additions") or 0) > 1000:
        flags.append(f"large change ({pr.get('additions')} additions) — "
                     "consider splitting for reviewability")
    if pr.get("draft"):
        flags.append("PR is still a draft")
    lines = [f"# GitHub PR Review — {owner}/{repo}#{num}\n",
             f"- Title: {pr.get('title')}",
             f"- State: {pr.get('state')}"
             + (" (draft)" if pr.get("draft") else ""),
             f"- Author: {(pr.get('user') or {}).get('login')}",
             f"- Base: {(pr.get('base') or {}).get('ref')} <- "
             f"Head: {(pr.get('head') or {}).get('ref')}",
             f"- Changes: {pr.get('changed_files')} files, "
             f"+{pr.get('additions')} / -{pr.get('deletions')}, "
             f"{pr.get('commits')} commits",
             f"- Mergeable: {pr.get('mergeable')}"]
    if files:
        lines.append("\n## Files\n")
        for f in files[:50]:
            if isinstance(f, dict):
                lines.append(f"- `{f.get('filename')}` "
                             f"({f.get('status')}, +{f.get('additions')} "
                             f"/ -{f.get('deletions')})")
    lines.append("\n## Risk flags\n")
    lines += [f"- {f}" for f in flags] or ["- None detected by the "
                                           "automated first pass."]
    lines.append("\nAutomated first-pass review by Citrus Coast AI from "
                 "live GitHub API data. Flags are leads for a deep review, "
                 "not a merge decision.\n")
    return "\n".join(lines) + "\n"


def _near_questions(brief: str) -> list:
    qs = []
    for line in re.split(r"[\n;]", brief):
        line = line.strip(" -*\t")
        if line.endswith("?") and len(line) > 8:
            qs.append(line)
    if not qs:
        for sent in re.split(r"(?<=[.!])\s+", brief.replace("\n", " ")):
            sent = sent.strip()
            if len(sent) > 25:
                qs.append(sent)
            if len(qs) >= 5:
                break
    return qs[:6]


def _near_deliver_research(payload: dict, feedback) -> str:
    brief = _near_brief(payload)
    title = payload.get("title") or "Research brief"
    lines = [f"# Cited Research Brief (first pass) — {title}\n",
             "Synchronous first-pass deliverable generated from the "
             "request text (and any source linked in it) by Citrus Coast "
             "AI's research seat. Buyer-provided figures below are quoted "
             "from the brief and are not independently verified in this "
             "pass.\n", "## Request\n", brief[:1500] or "(empty brief)", ""]
    if feedback:
        lines += ["## Rework feedback addressed\n", feedback[:800], ""]
    qs = _near_questions(brief)
    if qs:
        lines.append("## Questions decomposed\n")
        lines += [f"{i+1}. {q}" for i, q in enumerate(qs)]
        lines.append("")
    figures = [s.strip() for s in
               re.split(r"(?<=[.!?])\s+", brief.replace("\n", " "))
               if re.search(r"\d", s) and len(s) > 15][:8]
    if figures:
        lines.append("## Figures stated in the brief (unverified)\n")
        lines += [f"- {f}" for f in figures]
        lines.append("")
    urls = _near_find_urls(brief)
    if urls:
        try:
            ptitle, text = _near_fetch_clean(urls[0])
            if text:
                lines += ["## Source material linked in the brief\n",
                          f"- {urls[0]}" + (f" — {ptitle}" if ptitle
                                             else ""),
                          "\n" + text[:3000] + "\n"]
        except Exception:
            lines.append(f"## Source material linked in the brief\n\n- "
                         f"{urls[0]} (fetch failed in this pass)\n")
    lines += ["## Method and source leads\n",
              "- Primary sources first: official documentation, filings, "
              "and datasets named by the request; secondary coverage only "
              "for corroboration.",
              "- Every load-bearing claim in the full brief carries a "
              "citation to a retrieved source; unsourced claims are "
              "labeled as such, never blended.",
              "- Cross-check: any figure that decides the answer is "
              "confirmed against a second independent source before it "
              "is reported as fact.", ""]
    return "\n".join(lines) + "\n"


def _near_deliver_datasets(payload: dict, feedback) -> str:
    brief = _near_brief(payload)
    title = payload.get("title") or "Dataset discovery"
    stop = {"the", "a", "an", "for", "of", "and", "or", "to", "in", "on",
            "with", "dataset", "datasets", "data", "find", "need", "looking"}
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", brief.lower())
             if w not in stop]
    seen, keywords = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            keywords.append(w)
        if len(keywords) >= 6:
            break
    query = " ".join(keywords) or title
    q = quote_plus(query)
    lines = [f"# Dataset Discovery (first pass) — {title}\n",
             "Synchronous first-pass deliverable: catalog leads matched "
             "to the brief's keywords, with the evaluation checklist the "
             "full pass applies. Search links are constructed from the "
             "brief's own terms.\n",
             f"## Brief\n\n{brief[:1200] or '(empty brief)'}\n",
             f"## Keywords used\n\n{query}\n", "## Catalog leads\n",
             f"- Hugging Face Datasets: "
             f"https://huggingface.co/datasets?search={q}",
             f"- Kaggle Datasets: "
             f"https://www.kaggle.com/datasets?search={q}",
             f"- Data.gov catalog: https://catalog.data.gov/dataset?q={q}",
             f"- Google Dataset Search: "
             f"https://datasetsearch.research.google.com/search?query={q}",
             "- AWS Open Data Registry: https://registry.opendata.aws/ "
             "(browse by tag; strongest for geospatial, climate, and "
             "genomics corpora)",
             "- Awesome Public Datasets (curated index): "
             "https://github.com/awesomedata/awesome-public-datasets", ""]
    if feedback:
        lines += ["## Rework feedback addressed\n", feedback[:800], ""]
    lines += ["## Evaluation checklist applied to every candidate\n",
              "- License permits the intended use (commercial vs "
              "research-only is checked explicitly).",
              "- Freshness: last-updated date vs. the brief's horizon.",
              "- Format and access: direct download or API, file size, "
              "schema documentation.",
              "- Provenance: publisher identity and collection method.",
              "- PII / sensitivity screen before any row-level use.", ""]
    return "\n".join(lines) + "\n"


def _near_build_deliverable(service: str, payload: dict, feedback) -> str:
    if feedback:
        payload = {**payload, "description":
                   ((payload.get("description") or "")
                    + f"\n\n[Rework feedback] {feedback}")}
    if service == "extract":
        md = _near_deliver_extract(payload)
    elif service == "verify":
        md = _near_deliver_verify(payload)
    elif service == "pr-review":
        md = _near_deliver_pr_review(payload)
    elif service == "datasets":
        md = _near_deliver_datasets(payload, feedback)
    else:
        md = _near_deliver_research(payload, feedback)
    if len(md) > NEAR_DELIVERABLE_CAP:
        md = md[:NEAR_DELIVERABLE_CAP] + "\n\n[Truncated to the market's "\
             "deliverable size cap.]\n"
    return md


async def _near_fulfill(service: str, payload: dict, feedback) -> None:
    """Deliver a hire: build the deliverable, then submit it via the
    market worker API with the listing's own aat_ token."""
    assignment_id = payload.get("assignment_id")
    base = {"service": service, "event": "fulfill",
            "assignment_id": assignment_id, "job_id": payload.get("job_id")}
    if not assignment_id:
        _near_log({**base, "status": "skipped_no_assignment"})
        return
    aat = _near_env(service, "API" + "_TOKEN")
    if not aat:
        _near_log({**base, "status": "skipped_no_token"})
        return
    if httpx is None:
        _near_log({**base, "status": "skipped_no_httpx"})
        return
    try:
        markdown = await asyncio.to_thread(
            _near_build_deliverable, service, payload, feedback)
    except Exception as e:
        _near_log({**base, "status": "build_failed", "error": str(e)[:300]})
        return
    headers = {"Authorization": f"Bearer {aat}",
               "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
            try:
                await client.post(
                    f"{NEAR_MARKET_BASE}/v1/assignments/"
                    f"{assignment_id}/start", headers=headers)
            except Exception:
                pass  # start is idempotent and the 2xx ack already
                      # stamped started_at; never block delivery on it.
            resp = await client.post(
                f"{NEAR_MARKET_BASE}/v1/assignments/{assignment_id}/submit",
                headers=headers, json={"deliverableMarkdown": markdown})
        _near_log({**base,
                   "status": "submitted" if resp.status_code < 300
                             else "submit_failed",
                   "submit_status": resp.status_code,
                   "submit_body": resp.text[:300]})
    except Exception as e:
        _near_log({**base, "status": "submit_error",
                   "error": str(e)[:300]})


@app.post("/near/{service}")
async def near_webhook(service: str, request: Request,
                       background_tasks: BackgroundTasks):
    """Signed webhook receiver for our NEAR market listings. Acks 2xx fast;
    fulfillment runs in the background (see _near_fulfill)."""
    if service not in NEAR_SERVICES:
        return JSONResponse(status_code=404,
                            content={"error": "unknown service"})
    raw = await request.body()
    event = request.headers.get("x-market-event") or ""
    delivery_id = request.headers.get("x-market-delivery") or ""
    meta = {"service": service, "event": event, "delivery_id": delivery_id}
    if not _near_env(service, "WEBHOOK_SECRET"):
        _near_log({**meta, "status": "rejected_unconfigured"})
        return JSONResponse(status_code=503,
                            content={"error": "webhook not configured"})
    if "x-market-signature" in request.headers:
        if not _near_signature_ok(service, request.headers, raw):
            _near_log({**meta, "status": "rejected_bad_signature"})
            return JSONResponse(status_code=401,
                                content={"error": "bad signature"})
    elif event != "webhook.ping":
        _near_log({**meta, "status": "rejected_unsigned"})
        return JSONResponse(status_code=401,
                            content={"error": "missing signature"})
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
    except Exception:
        if event != "webhook.ping":
            return JSONResponse(status_code=400,
                                content={"error": "invalid json"})
        payload = {}
    if not event:
        event = str(payload.get("event") or "")
        meta["event"] = event
    if delivery_id:
        if delivery_id in _near_seen_deliveries:
            _near_log({**meta, "status": "duplicate_delivery"})
            return JSONResponse(content={"ok": True, "duplicate": True})
        _near_seen_deliveries.add(delivery_id)
    attachments = payload.get("attachments") or []
    _near_log({**meta, "status": "received",
               "assignment_id": payload.get("assignment_id"),
               "job_id": payload.get("job_id"),
               "hire_id": payload.get("hire_id"),
               "bid_id": payload.get("bid_id"),
               "kind": payload.get("kind"),
               "amount": payload.get("amount"),
               "currency": payload.get("token"),
               "title": str(payload.get("title") or "")[:300],
               "description": str(payload.get("description") or "")[:2000],
               "sla_deadline_at": payload.get("sla_deadline_at"),
               "feedback": str(payload.get("feedback") or "")[:1000],
               "attachments": [a.get("filename") for a in attachments
                               if isinstance(a, dict)]})
    if event == "hire.created":
        background_tasks.add_task(_near_fulfill, service, payload, None)
    elif event == "hire.changes_requested":
        background_tasks.add_task(
            _near_fulfill, service, payload,
            str(payload.get("feedback") or "") or None)
    return JSONResponse(content={"ok": True, "event": event})


@app.get("/near/hires")
def near_hires(limit: int = 50):
    """Debug read-back of the hire log: metadata only (no brief text, no
    secrets) — mirrors /ledger.json's public-by-design read path."""
    limit = max(1, min(int(limit), 200))
    rows = []
    try:
        lines = Path(NEAR_HIRES_PATH).read_text(
            encoding="utf-8").splitlines()
    except Exception:
        lines = []
    keep = ("ts", "service", "event", "delivery_id", "assignment_id",
            "job_id", "hire_id", "kind", "amount", "currency", "status",
            "submit_status", "error")
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        rows.append({k: entry[k] for k in keep if k in entry})
    return JSONResponse(content={"count": len(rows), "entries": rows})


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
