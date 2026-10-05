"""Dibuja el ícono de Vortexia Prospector (una V blanca sobre el morado de la página).

Uso: uv run python tools/crear-icono.py
Genera app/web/static/vortexia.ico (Windows, varios tamaños) y app/web/static/vortexia.png.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

STATIC = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
SIZE = 256
PURPLE = (139, 108, 255)  # --accent de la página
DARK = (14, 16, 21)


def font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)
    canvas.rounded_rectangle((8, 8, SIZE - 8, SIZE - 8), radius=56, fill=PURPLE)
    canvas.rounded_rectangle((8, 150, SIZE - 8, SIZE - 8), radius=56, fill=(120, 88, 240))  # sombra suave abajo
    canvas.rectangle((8, 150, SIZE - 8, 190), fill=(120, 88, 240))
    text = "V"
    letter = font(190)
    left, top, right, bottom = canvas.textbbox((0, 0), text, font=letter)
    x = (SIZE - (right - left)) / 2 - left
    y = (SIZE - (bottom - top)) / 2 - top
    canvas.text((x + 4, y + 6), text, font=letter, fill=(*DARK, 90))  # sombra de la letra
    canvas.text((x, y), text, font=letter, fill=(255, 255, 255))
    return image


def main() -> None:
    image = draw()
    image.save(STATIC / "vortexia.png")
    image.save(STATIC / "vortexia.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Listo:", STATIC / "vortexia.ico")


if __name__ == "__main__":
    main()
