from PIL import Image, ImageDraw


def make_placeholder(panel_size: int, label: str, color: str) -> Image.Image:
    img = Image.new("RGBA", (panel_size, panel_size), color)
    draw = ImageDraw.Draw(img)
    text = label.upper()
    bbox = draw.textbbox((0, 0), text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((panel_size - tw) / 2, (panel_size - th) / 2), text, fill="white")
    return img
