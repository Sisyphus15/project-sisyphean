import json
from pathlib import Path

from atlas.models import AtlasConfig


def _resolve_path(base_dir: Path, value: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    return p


def load_config(path: str | Path | None = None) -> AtlasConfig:
    base_dir = Path(__file__).parent
    cfg_path = Path(path) if path else base_dir / "atlas_config.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    panel_size = int(raw.get("panel_size", 256) or 256)
    crop_mode = str(raw.get("crop_mode", "center_square"))
    panel_keys = raw.get("panel_keys", {}) or {}

    paths = raw.get("paths", {}) or {}
    incoming_dir = _resolve_path(base_dir, paths.get("incoming_dir", "work/incoming"))
    panels_dir = _resolve_path(base_dir, paths.get("panels_dir", "work/panels"))
    output_dir = _resolve_path(base_dir, paths.get("output_dir", "work/output"))

    dash = raw.get("dashboard", {}) or {}
    dashboard_command = [str(x) for x in dash.get("command", ["python3", "ingest_maps.py"])]
    dashboard_cwd = _resolve_path(base_dir, dash.get("cwd", "../atlas_grid/rust-heatmap-dashboard"))
    dashboard_output = _resolve_path(base_dir, dash.get("output_path", "../atlas_grid/rust-heatmap-dashboard/output/dashboard.png"))
    dashboard_input_dir = _resolve_path(base_dir, dash.get("input_dir", "../atlas_grid/rust-heatmap-dashboard/input"))

    return AtlasConfig(
        panel_size=panel_size,
        crop_mode=crop_mode,
        panel_keys=panel_keys,
        incoming_dir=incoming_dir,
        panels_dir=panels_dir,
        output_dir=output_dir,
        dashboard_command=dashboard_command,
        dashboard_cwd=dashboard_cwd,
        dashboard_output=dashboard_output,
        dashboard_input_dir=dashboard_input_dir,
    )
