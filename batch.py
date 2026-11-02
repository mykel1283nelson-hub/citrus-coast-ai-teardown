"""Evidence Audit Batch — LOCAL BUILD ONLY (Board Decision 018-17, Days 2-3).

One $5 USDC payment buys up to 10 Evidence Audit analyses in a single
bounded batch, instead of ten separate $0.50 calls. This module is the
pure batch logic: validation, deterministic batch id, idempotent execution,
source grouping, and a machine-readable completion summary.

It REUSES the live Evidence Audit engine (`_run_evidence_check` from
~/workspace/ai-employee-agency/model-b/server.py) — it does not reimplement
corroboration, fetching, or confidence rules, and it does not invent
sources, citations, or truth verdicts. The engine's limitations carry over
verbatim: textual coverage is NOT semantic truth verification.

NOT A LIVE SERVICE. This module performs no payment, no settlement, and
no network call except the engine's own source fetches. The FastAPI route
in app.py (same folder) gates all payment behind EVIDENCE_BATCH_LIVE=1 and
returns 503 otherwise, so running this code can never charge anyone.
A separate release review must pass before any charge, listing, or deploy.

Replay / double-charge safety (the hard rule):
  * batch_id is a deterministic hash of the canonical audit list. The same
    audits always produce the same batch_id.
  * A completed batch is stored (BatchStore, atomic temp+rename writes).
  * run_batch() checks the store FIRST: a stored batch is returned with
    replayed=True and the engine is NOT re-run. The app route checks the
    store BEFORE verify/settle, so a replayed submission is never settled
    twice. The store is only written after a successful run (in the app
    flow, after settle) — a stored result therefore implies a prior
    completed run, never a free result.
  * Validation failures reject the WHOLE batch before any execution: no
    partial paid delivery, no half-executed batch to reconcile.
"""

import fcntl
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

# --- bounded commercial spec (018-17) ---
PRICE_USDC = 5.0
PRICE_ATOMIC = "5000000"  # $5 USDC, 6 decimals
MAX_AUDITS = 10
MAX_SOURCES_PER_AUDIT = 10
MAX_TOTAL_SOURCES = 50
MAX_INPUT_CHARS = 2000
MAX_SOURCE_URL_CHARS = 2048
ALLOWED_INPUT_TYPES = ("claim", "question", "url", "brief")
DEFAULT_MAX_SECONDS = 120.0

_MODEL_B_DIR = Path.home() / "workspace" / "ai-employee-agency" / "model-b"


def _default_engine(input_type, inp, sources):
    """The live Evidence Audit engine, imported lazily so this module stays
    importable for validation tests even without model-b on sys.path."""
    if str(_MODEL_B_DIR) not in sys.path:
        sys.path.insert(0, str(_MODEL_B_DIR))
    import server  # noqa: PLC0415 (live engine, reused not copied)

    return server._run_evidence_check(input_type, inp, sources)


def _normalize_host(host):
    try:
        if str(_MODEL_B_DIR) not in sys.path:
            sys.path.insert(0, str(_MODEL_B_DIR))
        import server  # noqa: PLC0415

        return server._normalize_evidence_host(host or "")
    except Exception:
        return (host or "").lower().lstrip("www.").strip(".")


def canonical_audits(audits):
    """Return the canonical form used for batch_id: only the fields the
    engine consumes, in a fixed order, with stripped strings."""
    out = []
    for a in audits:
        out.append({
            "input_type": a.get("input_type", "claim"),
            "input": a.get("input", ""),
            "sources": list(a.get("sources", [])),
        })
    return out


def batch_id_for(audits):
    payload = json.dumps(canonical_audits(audits), sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "eab_" + hashlib.sha256(payload).hexdigest()


def validate_audits(audits):
    """Return a list of per-audit error strings (empty = valid). Whole
    batch is rejected when any error exists; nothing is executed."""
    errors = []
    if not isinstance(audits, list):
        return ["audits must be a list"]
    if not audits:
        errors.append("audits must contain at least 1 audit")
    if len(audits) > MAX_AUDITS:
        errors.append(f"audits exceeds maximum of {MAX_AUDITS} (got {len(audits)})")
    total_sources = 0
    for i, a in enumerate(audits):
        tag = f"audits[{i}]"
        if not isinstance(a, dict):
            errors.append(f"{tag} must be an object")
            continue
        itype = a.get("input_type", "claim")
        if itype not in ALLOWED_INPUT_TYPES:
            errors.append(f"{tag}.input_type must be one of {ALLOWED_INPUT_TYPES}")
        inp = a.get("input", "")
        if not isinstance(inp, str):
            errors.append(f"{tag}.input must be a string")
        elif len(inp) > MAX_INPUT_CHARS:
            errors.append(f"{tag}.input exceeds {MAX_INPUT_CHARS} characters")
        elif itype != "brief" and not inp.strip():
            errors.append(f"{tag}.input must not be empty")
        sources = a.get("sources", [])
        if not isinstance(sources, list):
            errors.append(f"{tag}.sources must be a list")
            continue
        if len(sources) > MAX_SOURCES_PER_AUDIT:
            errors.append(f"{tag}.sources exceeds {MAX_SOURCES_PER_AUDIT}")
        total_sources += len(sources)
        for j, s in enumerate(sources):
            if not isinstance(s, str) or not s.startswith(("http://", "https://")):
                errors.append(f"{tag}.sources[{j}] must be an http(s) URL")
            elif len(s) > MAX_SOURCE_URL_CHARS:
                errors.append(f"{tag}.sources[{j}] exceeds {MAX_SOURCE_URL_CHARS} characters")
    if total_sources > MAX_TOTAL_SOURCES:
        errors.append(f"total sources exceeds {MAX_TOTAL_SOURCES} (got {total_sources})")
    return errors


class BatchStore:
    """File-backed completed-batch store. Writes are atomic (temp + rename)
    so a crashed run never leaves a half-written completed result.

    Durability note (honest): temp+rename + fsync survives a process
    restart on the SAME disk. It does NOT survive Render ephemeral-disk
    loss on redeploy or instance replacement — see RELEASE-REVIEW.md.
    The on-chain settlement transaction is the durable proof of payment;
    this store is the replay-protection record on the serving host.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = {}
        self._mtime = None
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(self._data, dict):
                    self._data = {}
                self._mtime = self.path.stat().st_mtime_ns
            except Exception:
                self._data = {}
                self._mtime = None

    def get(self, batch_id):
        # Re-read if another request/process wrote since this object was
        # constructed. Without this, a request that starts before a
        # concurrent request stores its result holds a stale empty cache
        # and misses the replay — it would 409 instead of replaying.
        try:
            mtime = self.path.stat().st_mtime_ns if self.path.exists() else None
        except Exception:
            mtime = None
        if mtime != self._mtime:
            self._load()
        return self._data.get(batch_id)

    def put(self, batch_id, result):
        self._data[batch_id] = result
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._data, separators=(",", ":")))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        try:
            dfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except Exception:
            pass


class PartialStore:
    """File-backed PARTIAL-batch store. A partial result is stored here
    and ONLY here — never in BatchStore, never served as complete.

    Purpose: the no-charge retry rule. A buyer who paid $5 and got a
    partial batch (time budget) must be able to finish the remaining
    audits without paying again. The partial record is what the retry
    path uses to know which audits are still 'not_run'. If a partial
    can never complete, the manual refund path (README) uses this record
    plus the receipt's settlement tx — a partial is never silently
    kept, and never upgraded to complete without actually running the
    remaining audits."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = {}
        self._mtime = None
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(self._data, dict):
                    self._data = {}
                self._mtime = self.path.stat().st_mtime_ns
            except Exception:
                self._data = {}
                self._mtime = None

    def get(self, batch_id):
        try:
            mtime = self.path.stat().st_mtime_ns if self.path.exists() else None
        except Exception:
            mtime = None
        if mtime != self._mtime:
            self._load()
        return self._data.get(batch_id)

    def put(self, batch_id, result):
        self._data[batch_id] = result
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._data, separators=(",", ":")))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def delete(self, batch_id):
        if batch_id in self._data:
            del self._data[batch_id]
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._data, separators=(",", ":")))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)


def run_batch_remaining(partial, audits, engine=None, store=None,
                        partial_store=None, max_seconds=DEFAULT_MAX_SECONDS):
    """Retry only the 'not_run' audits of a partial batch, at no charge.

    The caller (app route) has already established that this batch_id
    has a partial record and that no new payment is being taken. This
    function never settles, never charges, and never returns a partial
    as complete: if audits remain 'not_run' after this retry, the
    updated partial is stored back in PartialStore and status stays
    'partial'. Only when every audit has run does the merged result
    move to the complete store (and out of PartialStore).
    """
    canon = canonical_audits(audits)
    for a in canon:
        a.setdefault("input_type", "claim")
    results = [dict(r) for r in partial.get("results", [])]
    engine = engine or _default_engine
    start = time.monotonic()
    still_pending = False
    for i, a in enumerate(canon):
        if i >= len(results) or results[i].get("status") != "not_run":
            continue
        if time.monotonic() - start > max_seconds:
            still_pending = True
            continue
        try:
            results[i] = engine(a["input_type"], a["input"], a["sources"])
        except Exception as e:
            results[i] = {
                "status": "error",
                "input_type": a["input_type"],
                "input": a["input"][:1000],
                "error": str(e),
            }
    if any(r.get("status") == "not_run" for r in results):
        still_pending = True
    counts_status, counts_conf = {}, {}
    for r in results:
        counts_status[r.get("status", "unknown")] = counts_status.get(r.get("status", "unknown"), 0) + 1
        if r.get("confidence"):
            counts_conf[r["confidence"]] = counts_conf.get(r["confidence"], 0) + 1
    completed = sum(1 for r in results if r.get("status") not in ("not_run",))
    merged = dict(partial)
    merged.update({
        "status": "partial" if still_pending else "complete",
        "audit_count": len(results),
        "completed_count": completed,
        "replayed": False,
        "results": results,
        "source_groups": _source_groups(results),
        "summary": {
            "by_status": counts_status,
            "by_confidence": counts_conf,
            "distinct_source_hosts": len(_source_groups(results)),
        },
    })
    if still_pending:
        if partial_store is not None:
            partial_store.put(merged["batch_id"], merged)
    else:
        if partial_store is not None:
            partial_store.delete(merged["batch_id"])
        if store is not None:
            store.put(merged["batch_id"], merged)
    return merged


def _source_groups(results):
    """Group audit indices by the host of each fetched source."""
    groups = {}
    for idx, r in enumerate(results):
        if r.get("status") == "not_run":
            continue
        for s in r.get("sources", []) or []:
            if isinstance(s, dict) and s.get("status") == "fetched":
                host = _normalize_host(s.get("host") or "")
                if host:
                    groups.setdefault(host, [])
                    if idx not in groups[host]:
                        groups[host].append(idx)
    return {h: sorted(v) for h, v in sorted(groups.items())}


def run_batch(audits, engine=None, store=None, max_seconds=DEFAULT_MAX_SECONDS):
    """Execute a validated batch. Returns the completion summary dict.

    On validation failure: status "rejected", no audit executed, no store
    write. On store hit: the stored result is returned with replayed=True
    and the engine is not called. On time-budget exhaustion: remaining
    audits are "not_run" and status is "partial" — never "complete".
    """
    errors = validate_audits(audits)
    if errors:
        return {
            "status": "rejected",
            "price_usdc": PRICE_USDC,
            "max_audits": MAX_AUDITS,
            "audit_count": len(audits) if isinstance(audits, list) else 0,
            "errors": errors,
            "results": [],
            "summary": {},
        }
    canon = canonical_audits(audits)
    # Fill defaults the engine route applies: input_type defaults to claim.
    for a in canon:
        a.setdefault("input_type", "claim")
    bid = batch_id_for(canon)
    if store is not None:
        stored = store.get(bid)
        if stored is not None:
            replay = dict(stored)
            replay["replayed"] = True
            return replay
    engine = engine or _default_engine
    start = time.monotonic()
    results = []
    timed_out = False
    for a in canon:
        if time.monotonic() - start > max_seconds:
            timed_out = True
            results.append({
                "status": "not_run",
                "input_type": a["input_type"],
                "input": a["input"][:1000],
                "reason": "batch time budget exceeded; remaining audits not run",
            })
            continue
        try:
            results.append(engine(a["input_type"], a["input"], a["sources"]))
        except Exception as e:  # engine failure is a per-audit failure, not a crash
            results.append({
                "status": "error",
                "input_type": a["input_type"],
                "input": a["input"][:1000],
                "error": str(e),
            })
    counts_status, counts_conf = {}, {}
    for r in results:
        counts_status[r.get("status", "unknown")] = counts_status.get(r.get("status", "unknown"), 0) + 1
        if r.get("confidence"):
            counts_conf[r["confidence"]] = counts_conf.get(r["confidence"], 0) + 1
    completed = sum(1 for r in results if r.get("status") not in ("not_run",))
    result = {
        "batch_id": bid,
        "status": "partial" if timed_out else "complete",
        "price_usdc": PRICE_USDC,
        "max_audits": MAX_AUDITS,
        "audit_count": len(results),
        "completed_count": completed,
        "replayed": False,
        "results": results,
        "source_groups": _source_groups(results),
        "summary": {
            "by_status": counts_status,
            "by_confidence": counts_conf,
            "distinct_source_hosts": len(_source_groups(results)),
        },
        "limitations": [
            "Textual evidence coverage only: confidence reports coverage, never that a claim is verified or true.",
            "Only caller-supplied sources were used; no independent source discovery was performed.",
            "Not medical, legal, or financial advice.",
        ],
    }
    if store is not None and not timed_out:
        # Only complete batches are stored as replayable: a partial batch
        # must be re-run (as a new batch_id if its audits change, or retried
        # by the caller), never silently replayed as complete.
        store.put(bid, result)
    return result


# --- Live route handler (added at deploy time, 2026-11-02) ---
# The live server.py calls serve_batch() from a worker thread with the
# live payment helpers passed in, so this module never imports server.py
# at module load (no circular import) and never touches payment code
# except through the callables the server supplies. Logic mirrors the
# staging app.py route exactly: validate -> store replay check -> retry
# path -> partial 409 -> claim -> verify -> settle -> run -> store.
_batch_lock = threading.Lock()
_BATCH_CLAIM_FRESH_SECONDS = 600


def serve_batch(body, headers, verify, settle, requirements, pay_to, network, base_dir):
    """Serve one Evidence Audit Batch request. Returns a (status, content_dict, extra_headers) tuple."""
    import base64 as _b64
    base_dir = Path(base_dir)
    store_path = base_dir / "batch_store.json"
    partial_path = base_dir / "batch_partial.json"
    receipts_path = base_dir / "batch_receipts.jsonl"
    claims_dir = base_dir / "claims"
    lock_path = base_dir / ".batch_lock"

    def challenge():
        return (402, {"x402Version": 1, "accepts": [requirements], "extensions": {"bazaar": {"discoverable": True, "name": "Citrus Coast Evidence Audit Batch", "description": "Up to 10 Evidence Audit analyses in one $5 USDC batch. Caller-supplied sources only; textual coverage, not truth verification."}}, "error": "payment required: $5 USDC on Base for the Evidence Audit Batch"}, {"PAYMENT-REQUIRED": _b64.b64encode(json.dumps({"x402Version": 1, "accepts": [requirements]}).encode()).decode()})

    def log_receipt(entry):
        try:
            with open(receipts_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            print(f"batch receipt write failed: {e}", file=sys.stderr)

    def claim_path(bid):
        return claims_dir / (bid + ".json")

    audits = body.get("audits") if isinstance(body, dict) else None
    errors = validate_audits(audits)
    if errors:
        return (400, {"error": "invalid batch", "errors": errors}, {})
    with _batch_lock:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            store = BatchStore(store_path)
            partial_store = PartialStore(partial_path)
            bid = batch_id_for(canonical_audits(audits))
            stored = store.get(bid)
            if stored is not None:
                replay = dict(stored)
                replay["replayed"] = True
                return (200, replay, {})
            retry_bid = headers.get("x-batch-retry")
            if retry_bid is not None:
                if retry_bid != bid:
                    return (400, {"error": "x-batch-retry batch_id does not match request body", "batch_id": bid}, {})
                partial = partial_store.get(bid)
                if partial is None:
                    return (409, {"error": "no partial batch to retry for this batch_id", "batch_id": bid}, {})
                merged = run_batch_remaining(partial, audits, store=store, partial_store=partial_store)
                log_receipt({"agent": "citruscoastai", "direction": "retry", "resource": "/api/evidence-batch", "amount": 0, "asset": "USDC", "txid": (merged.get("settlement") or {}).get("transaction"), "batch_id": bid, "audit_count": merged.get("audit_count"), "status": merged.get("status")})
                return (200, merged, {})
            partial = partial_store.get(bid)
            if partial is not None:
                return (409, {"error": "batch partially completed; retry the remaining audits at no charge with header x-batch-retry: <batch_id> and no new payment", "batch_id": bid, "completed_count": partial.get("completed_count"), "status": "partial"}, {})
            x_payment = headers.get("x-payment")
            if not x_payment:
                return challenge()
            try:
                payload = json.loads(_b64.b64decode(x_payment).decode())
            except Exception:
                return (402, {"error": "malformed X-PAYMENT header"}, {})
            try:
                existing = json.loads(claim_path(bid).read_text(encoding="utf-8"))
            except Exception:
                existing = None
            if existing is not None:
                age = time.time() - float(existing.get("claimed_at") or 0)
                if age <= _BATCH_CLAIM_FRESH_SECONDS:
                    return (409, {"error": "batch already in flight; do not resubmit payment", "batch_id": bid}, {})
                return (409, {"error": "batch claim stale with no completed result; manual reconciliation required; do not resubmit payment", "batch_id": bid}, {})
            payer_hint = payload.get("payer") if isinstance(payload, dict) else None
            claims_dir.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(str(claim_path(bid)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                stored = store.get(bid)
                if stored is not None:
                    replay = dict(stored)
                    replay["replayed"] = True
                    return (200, replay, {})
                return (409, {"error": "batch already in flight; do not resubmit payment", "batch_id": bid}, {})
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"batch_id": bid, "payer": payer_hint, "claimed_at": time.time(), "status": "in-flight"}, separators=(",", ":")))
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                try:
                    claim_path(bid).unlink()
                except Exception:
                    pass
                return (409, {"error": "batch already in flight; do not resubmit payment", "batch_id": bid}, {})
            ok, detail = verify(payload, requirements)
            if not ok:
                try:
                    claim_path(bid).unlink()
                except Exception:
                    pass
                return (402, {"error": f"payment invalid: {detail}"}, {})
            settled, settle_resp, settle_detail = settle(payload, requirements)
            if not settled:
                try:
                    claim_path(bid).unlink()
                except Exception:
                    pass
                return (402, {"error": f"payment settlement failed: {settle_detail}"}, {})
            result = run_batch(audits, store=store)
            result["settlement"] = {"transaction": settle_resp.get("transaction"), "network": settle_resp.get("network") or network, "payer": settle_resp.get("payer")}
            if result.get("status") == "partial":
                partial_store.put(bid, result)
            else:
                store.put(bid, result)
            log_receipt({"agent": "citruscoastai", "direction": "sell", "resource": "/api/evidence-batch", "amount": PRICE_USDC, "asset": "USDC", "txid": settle_resp.get("transaction"), "network": settle_resp.get("network") or network, "payer": settle_resp.get("payer"), "pay_to": pay_to, "batch_id": bid, "audit_count": result.get("audit_count"), "status": result.get("status")})
            return (200, result, {"X-PAYMENT-RESPONSE": _b64.b64encode(json.dumps(settle_resp, separators=(",", ":")).encode()).decode()})
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            os.close(lock_fd)
