import json
import os
from datetime import datetime

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "audit.log")

os.makedirs(LOG_DIR, exist_ok=True)

def audit_log(event: str, user, details: dict):
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "event": event,
        "user_id": user.id if user else None,
        "username": str(user) if user else None,
        "details": details,
    }

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
