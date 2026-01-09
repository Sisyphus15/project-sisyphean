import shutil
import subprocess
import time
from pathlib import Path

from PIL import Image

from atlas.config import load_config
from atlas.models import AtlasConfig


def process_raw_screenshot(raw_path: str | Path, *, crop_mode: str, out_path: str | Path, panel_size: int) -> Path:
    src = Path(raw_path)
    dst = Path(out_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    img = Image.open(src).convert("RGBA")
    if crop_mode == "center_square":
        w, h = img.size
        side = min(w, h)
        left = int((w - side) / 2)
        top = int((h - side) / 2)
        img = img.crop((left, top, left + side, top + side))

    if panel_size:
        img = img.resize((panel_size, panel_size), Image.LANCZOS)

    img.save(dst, format="PNG")
    return dst


def _copy_panels_to_dashboard_input(cfg: AtlasConfig) -> None:
    cfg.dashboard_input_dir.mkdir(parents=True, exist_ok=True)
    for panel_path in cfg.panels_dir.glob("*.png"):
        target = cfg.dashboard_input_dir / panel_path.name
        shutil.copy2(panel_path, target)


def normalize_and_place(raw_path: str | Path, panel_key: str, *, cfg: AtlasConfig | None = None) -> Path:
    cfg = cfg or load_config()
    if panel_key not in cfg.panel_keys:
        raise ValueError(f"Unknown panel key: {panel_key}")

    raw_path = Path(raw_path)
    cfg.incoming_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    incoming_path = cfg.incoming_dir / f"{ts}_{raw_path.name}"
    shutil.copy2(raw_path, incoming_path)

    output_name = cfg.panel_keys[panel_key]
    output_path = cfg.panels_dir / output_name
    return process_raw_screenshot(
        incoming_path,
        crop_mode=cfg.crop_mode,
        out_path=output_path,
        panel_size=cfg.panel_size,
    )


def build_dashboard(*, cfg: AtlasConfig | None = None) -> Path:
    cfg = cfg or load_config()
    _copy_panels_to_dashboard_input(cfg)

    proc = subprocess.run(
        cfg.dashboard_command,
        cwd=str(cfg.dashboard_cwd),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        raise RuntimeError(f"Atlas dashboard build failed (code {proc.returncode}):\n{log}")

    return cfg.dashboard_output
