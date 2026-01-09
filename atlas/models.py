from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class AtlasConfig:
    panel_size: int
    crop_mode: str
    panel_keys: Mapping[str, str]
    incoming_dir: Path
    panels_dir: Path
    output_dir: Path
    dashboard_command: list[str]
    dashboard_cwd: Path
    dashboard_output: Path
    dashboard_input_dir: Path
