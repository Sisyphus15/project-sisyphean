import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "rust_config.json")

DEFAULT_CONFIG = {
    "server_name": "",
    "server_ip": "",
    "server_port": 28015,
    "player_id": "",
    "player_token": "",
    "smart_alarm_id": "",
    "f1_connect": ""
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return DEFAULT_CONFIG.copy()

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    cfg = DEFAULT_CONFIG.copy()
    cfg.update(data)  # merge any existing keys
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"\n✅ Saved config to {CONFIG_PATH}\n")


def prompt(text, default=None, cast=str):
    """
    Simple input helper:
    - Shows [default] if provided
    - Returns default if user hits Enter
    - Optionally casts to int, etc.
    """
    if default not in (None, ""):
        full = f"{text} [{default}]: "
    else:
        full = f"{text}: "

    raw = input(full).strip()
    if not raw and default is not None:
        return default

    if cast is int:
        try:
            return int(raw)
        except ValueError:
            print("⚠️ Invalid integer, keeping previous value.")
            return default

    return raw


def edit_server_info(cfg):
    print("\n--- Edit Server Info ---")
    cfg["server_name"] = prompt("Server name", cfg.get("server_name", ""))
    cfg["server_ip"] = prompt("Server IP", cfg.get("server_ip", ""))
    cfg["server_port"] = prompt(
        "Server port",
        cfg.get("server_port", 28015),
        cast=int,
    )
    return cfg


def edit_rustplus_info(cfg):
    print("\n--- Edit Rust+ Info ---")
    cfg["player_id"] = prompt("player_id", cfg.get("player_id", ""))
    cfg["player_token"] = prompt("player_token", cfg.get("player_token", ""))
    cfg["smart_alarm_id"] = prompt(
        "smart_alarm_id (or GUID)",
        cfg.get("smart_alarm_id", ""),
    )
    return cfg


def generate_f1_connect(cfg):
    print("\n--- Generate F1 Connect Command ---")

    ip = cfg.get("server_ip", "").strip()
    port = cfg.get("server_port", 0)

    if not ip or not port:
        print("❌ You must set server_ip and server_port first.")
        return cfg

    cmd = f"connect {ip}:{port}"
    print(f"\nSuggested F1 command:\n  {cmd}\n")

    answer = input("Save this as f1_connect in config.json? [y/N]: ").strip().lower()
    if answer == "y":
        cfg["f1_connect"] = cmd
        print("✅ f1_connect updated.")
    else:
        print("ℹ️ f1_connect not changed.")

    return cfg


def show_config(cfg):
    print("\n--- Current rust_config.json ---")
    print(json.dumps(cfg, indent=2))
    print()


def main():
    cfg = load_config()

    while True:
        print("==== Project Sisyphean Config Tool ====")
        print("1) View current config")
        print("2) Edit server info (IP, port, name)")
        print("3) Edit Rust+ info (player_id, token, smart_alarm_id)")
        print("4) Generate & store F1 console connect command")
        print("5) Save and exit")
        print("6) Exit without saving")
        choice = input("Select an option: ").strip()

        if choice == "1":
            show_config(cfg)
        elif choice == "2":
            cfg = edit_server_info(cfg)
        elif choice == "3":
            cfg = edit_rustplus_info(cfg)
        elif choice == "4":
            cfg = generate_f1_connect(cfg)
        elif choice == "5":
            save_config(cfg)
            break
        elif choice == "6":
            print("Exiting without saving.")
            break
        else:
            print("❌ Invalid option, try again.\n")


if __name__ == "__main__":
    main()

