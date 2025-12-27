import json
import hashlib

LOG_FILE = "logs/audit.log"

def canonical(base: dict) -> str:
    # Must match audit_logger.py canonicalization
    import json as _json
    return _json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

prev = "0" * 64
ok = True
i = 0
verified_count = 0
skipped_legacy = 0

with open(LOG_FILE, "r", encoding="utf-8") as f:
    for line in f:
        i += 1
        line = line.strip()
        if not line:
            continue

        entry = json.loads(line)

        # Legacy entries won't have chain_hash fields. Skip them cleanly.
        if "chain_hash" not in entry or "prev_chain_hash" not in entry:
            skipped_legacy += 1
            continue

        # Build the signed payload exactly like audit_logger.py
        base = {
            "timestamp": entry.get("timestamp"),
            "event": entry.get("event"),
            "critical": entry.get("critical", False),   # default for older-but-signed entries
            "user_id": entry.get("user_id"),
            "username": entry.get("username"),
            "details": entry.get("details", {}),
        }

        payload = canonical(base)
        expected_chain = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()

        if entry.get("prev_chain_hash") != prev or entry.get("chain_hash") != expected_chain:
            ok = False
            print(f"❌ Chain broken at line {i}")
            print(f"   expected prev={prev}")
            print(f"   found    prev={entry.get('prev_chain_hash')}")
            print(f"   expected chain={expected_chain}")
            print(f"   found    chain={entry.get('chain_hash')}")
            break

        prev = entry["chain_hash"]
        verified_count += 1

if ok:
    print(f"✅ OK (verified {verified_count} signed entries, skipped {skipped_legacy} legacy entries)")
else:
    print("❌ FAIL")