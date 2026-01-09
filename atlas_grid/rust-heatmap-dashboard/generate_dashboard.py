import math
import os

from PIL import Image

from generate_icons import make_placeholder


def build_dashboard(config: dict, input_dir: str, output_path: str) -> None:
    panel_size = int(config.get("panel_size", 256) or 256)
    grid = config.get("grid", {}) or {}
    columns = int(grid.get("columns", 4) or 4)
    gap = int(grid.get("gap", 12) or 12)
    padding = int(grid.get("padding", 24) or 24)
    background_color = grid.get("background_color", "#0f1519")

    slot_order = config.get("slot_order", []) or []
    placeholder_color = config.get("placeholder_color", "#2a3238")

    rows = max(1, math.ceil(len(slot_order) / columns)) if slot_order else 1
    width = padding * 2 + columns * panel_size + gap * (columns - 1)
    height = padding * 2 + rows * panel_size + gap * (rows - 1)

    canvas = Image.new("RGBA", (width, height), background_color)

    for idx, slot_key in enumerate(slot_order):
        row = idx // columns
        col = idx % columns
        x = padding + col * (panel_size + gap)
        y = padding + row * (panel_size + gap)

        img_path = os.path.join(input_dir, f"{slot_key}.png")
        img = None
        if os.path.exists(img_path):
            try:
                img = Image.open(img_path).convert("RGBA")
            except Exception:
                img = None

        if img is None:
            img = make_placeholder(panel_size, slot_key, placeholder_color)
        else:
            if img.size != (panel_size, panel_size):
                img = img.resize((panel_size, panel_size), Image.LANCZOS)

        canvas.paste(img, (x, y), img)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    canvas.save(output_path, format="PNG")
