import json
import os
import sys

from generate_dashboard import build_dashboard


def main() -> int:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, "config.json")
    input_dir = os.path.join(base_dir, "input")
    output_path = os.path.join(base_dir, "output", "dashboard.png")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as exc:
        print(f"Failed to read config.json: {exc}", file=sys.stderr)
        return 1

    try:
        build_dashboard(cfg, input_dir, output_path)
    except Exception as exc:
        print(f"Failed to build dashboard: {exc}", file=sys.stderr)
        return 2

    print(f"OK: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
