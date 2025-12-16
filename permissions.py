import json
import os
from audit_logger import audit_log

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "staff_config.json")


def load_permissions():
    if not os.path.exists(CONFIG_PATH):
        return {}

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("permissions", {})


def has_permission(interaction, permission_key: str) -> bool:
    permissions = load_permissions()
    allowed_roles = permissions.get(permission_key, [])

    if not allowed_roles:
        audit_log(
            "permission_denied",
            interaction.user,
            {"reason": "no_roles_configured", "permission": permission_key},
        )
        return False

    user_role_ids = {role.id for role in interaction.user.roles}

    if user_role_ids.intersection(set(allowed_roles)):
        return True

    audit_log(
        "permission_denied",
        interaction.user,
        {"reason": "missing_role", "permission": permission_key},
    )
    return False
