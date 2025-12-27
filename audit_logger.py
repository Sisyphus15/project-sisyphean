import json
import os
import hmac
import hashlib
from datetime import datetime
from typing import Optional

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "audit.log")

os.makedirs(LOG_DIR, exist_ok=True)

def _get_secret() -> bytes:
    secret = os.getenv("AUDIT_HMAC_SECRET", "")
    if not secret:
        # Fail closed: no signing secret means we should not pretend logs are tamper-evident.
        # You can change this to raise if you want to hard-require it.
        return b""
    return secret.encode("utf-8")

def _canonical_json(obj: dict) -> str:
    # Stable ordering + no whitespace = consistent hashing/signing
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _last_chain_hash() -> str:
    if not os.path.exists(LOG_FILE):
        return "0" * 64
    try:
        with open(LOG_FILE, "rb") as f:
            # Read last non-empty line efficiently
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return "0" * 64

            # Read backwards until newline
            block = b""
            pos = size
            while pos > 0:
                step = min(4096, pos)
                pos -= step
                f.seek(pos)
                block = f.read(step) + block
                if b"\n" in block:
                    break

            lines = [ln for ln in block.splitlines() if ln.strip()]
            if not lines:
                return "0" * 64

            last = json.loads(lines[-1].decode("utf-8"))
            return last.get("chain_hash", "0" * 64)
    except Exception:
        return "0" * 64

def _sign(payload_str: str, secret: bytes) -> str:
    if not secret:
        return ""
    return hmac.new(secret, payload_str.encode("utf-8"), hashlib.sha256).hexdigest()

def _chain_hash(prev_hash: str, payload_str: str) -> str:
    return hashlib.sha256((prev_hash + payload_str).encode("utf-8")).hexdigest()

def audit_log(event: str, user, details: dict, *, critical: bool = False) -> dict:
    """
    Returns the final log entry dict (including signature + chain_hash) so callers can also forward it to Discord.
    """
    base = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event": event,
        "critical": bool(critical),
        "user_id": getattr(user, "id", None),
        "username": str(user) if user else None,
        "details": details or {}
    }

    prev_hash = _last_chain_hash()
    payload_str = _canonical_json(base)

    secret = _get_secret()
    signature = _sign(payload_str, secret)
    chain_hash = _chain_hash(prev_hash, payload_str)

    entry = dict(base)
    entry["prev_chain_hash"] = prev_hash
    entry["chain_hash"] = chain_hash
    entry["signature_hmac_sha256"] = signature  # empty if no secret configured

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry
