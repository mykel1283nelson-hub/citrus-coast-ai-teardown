"""
Citrus Coast AI — Agent Trust Teardown engine.

Repositioned 2026-10-18 (Mykel): from "website health audit for local
businesses" to "deep trust report on an x402 agent endpoint." The ideology
is verify-before-you-pay, delivery-first: the verdict + score are always
free; the full per-check evidence is the $0.50 paid tier.

Input: an x402 endpoint URL (+ optional claimed terms to audit against).
Output: 6 weighted checks -> 0-100 trust score -> TRUST / CAUTION / DO NOT PAY.

$0 marginal cost per teardown: deterministic checks only, no LLM inference.
The check never pays the target endpoint and never signs anything.
"""
import time
from urllib.parse import urlparse

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NETWORK_CAIP2 = "eip155:8453"
NETWORK_LABELS = ("eip155:8453", "base")

# Weights must sum to 100.
WEIGHTS = {
    "liveness": 20,
    "challenge_parse": 20,
    "terms_audit": 15,
    "discovery_docs": 15,
    "identity_history": 10,
    "red_flags": 20,
}


def _server_helpers():
    """Lazy import of the fetch/parse helpers from server.py (avoids a
    circular import at module load: server.py imports this module at top
    level, so server is only imported here at call time)."""
    import server as _s
    return _s


def _check(name, weight, status, detail, evidence=None):
    """One check result. status: pass | warn | fail. Score contribution:
    pass = full weight, warn = half weight, fail = 0."""
    return {
        "check": name,
        "weight": weight,
        "status": status,
        "detail": detail,
        "evidence": evidence or {},
        "score": weight if status == "pass" else (weight // 2 if status == "warn" else 0),
    }


def _normalize_url(url):
    url = (url or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def run_trust_teardown(target_url, claimed=None):
    """Run the full trust teardown. claimed is an optional dict with any of
    {payTo, price_usdc, asset, network} the requester believes the endpoint
    advertises — we diff the live 402 against it. Returns a TrustReport."""
    S = _server_helpers()
    claimed = claimed or {}
    url = _normalize_url(target_url)
    parsed = urlparse(url)
    host = parsed.hostname or ""
    t0 = time.time()
    evidence_bank = {}

    # ---- 1. LIVENESS (GET + POST probe) ----
    get_status, post_status, get_ms, post_ms = None, None, None, None
    redirect_chain = []
    if not host or S._host_blocked(host):
        liveness = _check("liveness", WEIGHTS["liveness"], "fail",
                          "target host is missing or blocked — refusing to probe",
                          {"host": host})
    else:
        try:
            with S._check_http_client(host) as c:
                t = time.time()
                try:
                    r = c.get(url)
                    get_status, get_ms = r.status_code, int((time.time() - t) * 1000)
                    redirect_chain = [str(h.url) for h in r.history] + [str(r.url)]
                except Exception as e:
                    get_status = f"error: {e}"
                t = time.time()
                try:
                    r = c.post(url, json={})
                    post_status, post_ms = r.status_code, int((time.time() - t) * 1000)
                except Exception as e:
                    post_status = f"error: {e}"
        except Exception as e:
            get_status = f"client error: {e}"
        evidence_bank["probes"] = {
            "get_status": get_status, "get_ms": get_ms,
            "post_status": post_status, "post_ms": post_ms,
            "redirect_chain": redirect_chain,
        }
        ok_get = isinstance(get_status, int)
        ok_post = isinstance(post_status, int)
        if ok_get and ok_post:
            liveness = _check("liveness", WEIGHTS["liveness"], "pass",
                              f"endpoint responds (GET {get_status} in {get_ms}ms, "
                              f"POST {post_status} in {post_ms}ms)",
                              evidence_bank["probes"])
        elif ok_get or ok_post:
            liveness = _check("liveness", WEIGHTS["liveness"], "warn",
                              f"endpoint partially responds (GET {get_status}, POST {post_status})",
                              evidence_bank["probes"])
        else:
            liveness = _check("liveness", WEIGHTS["liveness"], "fail",
                              f"endpoint unreachable (GET {get_status}, POST {post_status})",
                              evidence_bank["probes"])

    # ---- 2. CHALLENGE PARSE (live 402) ----
    challenge, chal_warnings, chal_detail = None, [], ""
    if liveness["status"] != "fail":
        challenge, chal_warnings, chal_detail = S._fetch_live_402(url)
    if challenge:
        missing = [k for k in ("payTo", "asset", "network", "scheme")
                   if not challenge.get(k)]
        evidence_bank["challenge"] = {
            "parsed": challenge,
            "warnings": chal_warnings,
            "raw_host": challenge.get("host"),
            "final_url": challenge.get("final_url"),
        }
        if not missing:
            challenge_check = _check(
                "challenge_parse", WEIGHTS["challenge_parse"], "pass",
                f"live 402 parsed: {challenge['price_usdc']} USDC to "
                f"{challenge['payTo'][:10]}… on {challenge['network']}",
                evidence_bank["challenge"])
        else:
            challenge_check = _check(
                "challenge_parse", WEIGHTS["challenge_parse"], "warn",
                f"402 parsed but missing fields: {', '.join(missing)}",
                evidence_bank["challenge"])
    else:
        evidence_bank["challenge"] = {"parsed": None, "warnings": chal_warnings,
                                      "detail": chal_detail}
        challenge_check = _check(
            "challenge_parse", WEIGHTS["challenge_parse"], "fail",
            f"no parseable 402 challenge ({chal_detail})",
            evidence_bank["challenge"])

    # ---- 3. TERMS AUDIT ----
    terms_notes = []
    terms_status = "pass"
    if challenge:
        price = challenge.get("price_usdc")
        if price == 0:
            terms_notes.append("price is zero — unusual for a paid endpoint")
            terms_status = "warn"
        asset = (challenge.get("asset") or "").lower()
        if asset and asset != USDC_BASE.lower():
            terms_notes.append(f"asset is not Base USDC ({challenge.get('asset')})")
            terms_status = "warn"
        net = (challenge.get("network") or "").lower()
        if net and net not in NETWORK_LABELS:
            terms_notes.append(f"network is {challenge.get('network')}, not Base")
            terms_status = "warn"
        if (challenge.get("scheme") or "").lower() != "exact":
            terms_notes.append(f"scheme is {challenge.get('scheme')}, not 'exact'")
            terms_status = "warn"
        # Diff against claimed terms, when the requester supplied them.
        for field in ("payTo", "asset", "network"):
            claimed_v = claimed.get(field)
            live_v = challenge.get(field)
            if claimed_v and live_v and str(claimed_v).lower() != str(live_v).lower():
                terms_notes.append(f"claimed {field} ({claimed_v}) != live {field} ({live_v})")
                terms_status = "fail"
        claimed_price = claimed.get("price_usdc")
        if claimed_price is not None and price is not None:
            try:
                if abs(float(claimed_price) - float(price)) > 1e-9:
                    terms_notes.append(f"claimed price ({claimed_price}) != live price ({price})")
                    terms_status = "fail"
            except (TypeError, ValueError):
                pass
        if not terms_notes:
            terms_notes.append("terms sane; no claimed terms to diff"
                               if not claimed else "live terms match claimed terms")
    else:
        terms_status = "fail"
        terms_notes.append("no live challenge to audit")
    evidence_bank["terms"] = {"notes": terms_notes, "claimed": claimed}
    terms_check = _check("terms_audit", WEIGHTS["terms_audit"], terms_status,
                         "; ".join(terms_notes), evidence_bank["terms"])

    # ---- 4. DISCOVERY DOCS ----
    docs_notes = []
    docs_found = {}
    docs_status = "warn"  # no docs at all is a warn, not a fail
    if liveness["status"] != "fail":
        origin = f"{parsed.scheme}://{parsed.netloc}"
        try:
            with S._check_http_client(host) as c:
                for path in ("/.well-known/x402", "/.well-known/agent.json", "/skill.md"):
                    try:
                        r = c.get(origin + path)
                        found = r.status_code == 200
                        docs_found[path] = {
                            "status": r.status_code,
                            "found": found,
                            "bytes": len(r.content) if found else 0,
                            "content_truncated": (r.text[:2000] if found else None),
                        }
                        if found:
                            docs_notes.append(f"{path} present")
                    except Exception as e:
                        docs_found[path] = {"found": False, "error": str(e)}
            # Diff the x402 listing against the live challenge.
            if docs_found.get("/.well-known/x402", {}).get("found") and challenge:
                listing_match, listing_warnings = S._fetch_listing(url, challenge)
                evidence_bank["discovery"] = {"docs": docs_found,
                                              "listing_match": listing_match,
                                              "listing_warnings": listing_warnings}
                if listing_match == "matched":
                    docs_notes.append("public listing matches live 402")
                    docs_status = "pass"
                elif listing_match == "mismatched":
                    docs_notes.append("public listing MISMATCHES live 402: "
                                      + "; ".join(listing_warnings))
                    docs_status = "fail"
                else:
                    docs_notes.append(f"listing {listing_match}: "
                                      + "; ".join(listing_warnings))
                    docs_status = "warn"
            else:
                evidence_bank["discovery"] = {"docs": docs_found}
                if not any(d.get("found") for d in docs_found.values()):
                    docs_notes.append("no discovery docs found (/.well-known/x402, "
                                      "/.well-known/agent.json, /skill.md all missing)")
        except Exception as e:
            docs_notes.append(f"discovery fetch failed: {e}")
            evidence_bank["discovery"] = {"error": str(e)}
    else:
        docs_notes.append("endpoint unreachable — discovery docs not checked")
        evidence_bank["discovery"] = {"skipped": True}
    discovery_check = _check("discovery_docs", WEIGHTS["discovery_docs"], docs_status,
                             "; ".join(docs_notes) if docs_notes else "no notes",
                             evidence_bank["discovery"])

    # ---- 5. IDENTITY / HISTORY ----
    id_notes = []
    id_status = "warn"
    pay_to = (challenge or {}).get("payTo") or ""
    history_hits = []
    if pay_to:
        # Known operator: our own venture wallet.
        if pay_to.lower() == S.PAY_TO.lower():
            id_notes.append("payTo is the Citrus Coast AI venture wallet (our own endpoint)")
            id_status = "pass"
        else:
            # Best-effort local history: sell-side receipts on this host.
            try:
                import json as _json
                import os as _os
                receipts_path = _os.path.join(
                    _os.path.dirname(_os.path.abspath(S.__file__)), "sell_receipts.jsonl")
                if _os.path.exists(receipts_path):
                    with open(receipts_path, encoding="utf-8") as f:
                        for line in f:
                            try:
                                row = _json.loads(line)
                            except Exception:
                                continue
                            if (row.get("pay_to") or "").lower() == pay_to.lower():
                                history_hits.append({
                                    "txid": row.get("txid"),
                                    "amount": row.get("amount"),
                                    "resource": row.get("resource"),
                                    "observed_at": row.get("observed_at"),
                                })
            except Exception:
                pass
            if history_hits:
                id_notes.append(f"payTo seen in {len(history_hits)} local sell receipt(s)")
                id_status = "pass"
            else:
                id_notes.append("payTo has no history in our records — not proof of "
                                "malice, but no track record either")
    else:
        id_notes.append("no payTo to check (no live challenge)")
        id_status = "fail"
    evidence_bank["identity"] = {"payTo": pay_to, "history_hits": history_hits,
                                 "notes": id_notes}
    identity_check = _check("identity_history", WEIGHTS["identity_history"], id_status,
                            "; ".join(id_notes), evidence_bank["identity"])

    # ---- 6. RED FLAGS (aggregate) ----
    flags = []
    if liveness["status"] == "fail":
        flags.append("unreachable: endpoint did not respond to GET or POST")
    if challenge_check["status"] == "fail":
        flags.append("no parseable 402 challenge — cannot verify payment terms")
    if parsed.scheme == "http":
        flags.append("plain HTTP — payment terms could be tampered with in transit")
    req_host = (challenge or {}).get("requested_host") or ""
    fin_host = (challenge or {}).get("host") or ""
    if req_host and fin_host and req_host != fin_host:
        flags.append(f"redirects from {req_host} to {fin_host} — listing covers the requested host, not the final one")
    if challenge and not pay_to:
        flags.append("live 402 has no payTo address — do not pay it")
    if terms_check["status"] == "fail":
        flags.append("live terms mismatch claimed terms")
    if not any((d or {}).get("found") for d in docs_found.values()) and liveness["status"] != "fail":
        flags.append("no discovery docs (/.well-known/x402, /.well-known/agent.json, /skill.md all missing)")
    if challenge and challenge.get("price_usdc") == 0:
        flags.append("zero price on a paid endpoint — unusual")
    # Red-flags check: full weight when clean, scaled down per flag.
    rf_score = max(0, WEIGHTS["red_flags"] - 7 * len(flags))
    rf_status = "pass" if not flags else ("warn" if len(flags) <= 2 else "fail")
    evidence_bank["red_flags"] = {"flags": flags}
    redflag_check = {
        "check": "red_flags",
        "weight": WEIGHTS["red_flags"],
        "status": rf_status,
        "detail": "no red flags" if not flags else f"{len(flags)} red flag(s): " + "; ".join(flags),
        "evidence": evidence_bank["red_flags"],
        "score": rf_score,
    }

    checks = [liveness, challenge_check, terms_check, discovery_check,
              identity_check, redflag_check]
    score = max(0, min(100, sum(c["score"] for c in checks)))
    if score >= 80:
        verdict = "TRUST"
    elif score >= 50:
        verdict = "CAUTION"
    else:
        verdict = "DO NOT PAY"

    return TrustReport(
        url=url, requested_url=target_url, score=score, verdict=verdict,
        checks=checks, evidence_bank=evidence_bank,
        elapsed_ms=int((time.time() - t0) * 1000))


class TrustReport:
    """The teardown result. free_summary() is always free (delivery-first);
    full_report() is the paid tier (per-check evidence)."""

    def __init__(self, url, requested_url, score, verdict, checks, evidence_bank,
                 elapsed_ms):
        self.url = url
        self.requested_url = requested_url
        self.score = score
        self.verdict = verdict
        self.checks = checks
        self.evidence_bank = evidence_bank
        self.elapsed_ms = elapsed_ms
        self.checked_at = time.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00", time.gmtime())

    def free_summary(self):
        return {
            "service": "agent-trust-teardown",
            "url": self.requested_url,
            "final_url": self.url,
            "verdict": self.verdict,
            "trust_score": self.score,
            "checks": [
                {"check": c["check"], "status": c["status"], "detail": c["detail"]}
                for c in self.checks
            ],
            "checked_at": self.checked_at,
            "elapsed_ms": self.elapsed_ms,
            "generated_by": "Citrus Coast AI — Agent Trust Teardown (deterministic, no LLM)",
            "full_report": {
                "available": True,
                "price_usdc": 0.50,
                "how": "POST the same request with an X-PAYMENT header (x402, $0.50 USDC on Base)",
                "includes": ["raw 402 challenge", "probe timings + redirect chain",
                             "discovery doc contents", "terms field-by-field diff",
                             "payTo history hits"],
            },
        }

    def full_report(self):
        summary = self.free_summary()
        summary["evidence"] = {
            c["check"]: c["evidence"] for c in self.checks
        }
        summary["weights"] = WEIGHTS
        summary["check_scores"] = {c["check"]: c["score"] for c in self.checks}
        return summary
