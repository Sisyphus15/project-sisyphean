#!/usr/bin/env python3
import os
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "rust_config.json")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("Error reading existing config:", e)
        return {}


def ask(field: str, default=None, cast=str):
    if default is not None and default != "":
        prompt = f"{field} [{default}]: "
    else:
        prompt = f"{field}: "

    value = input(prompt).strip()
    if value == "":
        # keep existing
        return default
    try:
        return cast(value)
    except Exception:
        print(f"⚠️  Could not convert value for {field}, keeping previous.")
        return default


def main():
    print("=== Project Sisyphean :: Rust Config Wizard ===\n")

    cfg = load_config()
    if cfg:
        print("Current config:")
        for k, v in cfg.items():
            print(f"  {k}: {v}")
        print("")
    else:
        print("No existing rust_config.json found, creating a new one.\n")

    # Ask for values, keeping existing ones as defaults
    cfg["server_ip"] = ask(
        "Server IP (F1 shows this host)",
        cfg.get("server_ip", "us-2x-mon.rusticated.com"),
        str,
    )

    cfg["server_port"] = ask(
        "Server Port",
        cfg.get("server_port", 28010),
        int,
    )

    cfg["player_id"] = ask(
        "Rust+ player_id",
        cfg.get("player_id", ""),
        int,
    )

    cfg["player_token"] = ask(
        "Rust+ player_token",
        cfg.get("player_token", ""),
        str,
    )

    cfg["smart_alarm_id"] = ask(
        "Smart Alarm entity ID (for raid alerts)",
        cfg.get("smart_alarm_id", ""),
        int,
    )

    cfg["f1_connect"] = ask(
        "F1 connect command",
        cfg.get("f1_connect", "client.connect us-2x-mon.rusticated.com:28010"),
        str,
    )

    # Save
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    print(f"\n✅ Saved config to {CONFIG_PATH}")
    print("   Restart the bot service to apply changes:")
    print("   sudo systemctl restart sisyphus-bot\n")


if __name__ == "__main__":
    main()
