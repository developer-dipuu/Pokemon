from PIL import Image, ImageDraw
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).with_name("bp_grid.png")

img = Image.open(ROOT / 'assets' / 'battlepass.png').convert('RGBA')
draw = ImageDraw.Draw(img)
w, h = img.size
print(f"Image size: {w} x {h}")

# Draw a grid of colored reference points every 20px to identify areas
for x in range(0, w, 40):
    for y in range(0, h, 40):
        draw.point((x, y), fill=(255, 0, 0))

# Label key reference lines
# Horizontal lines at y=100,120,140,160,180,200,220,240,260,280,300
for y in [100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300]:
    draw.line([(0, y), (w, y)], fill=(255, 0, 0, 80), width=1)
    draw.text((5, y), f"y={y}", fill=(255, 0, 0))

# Vertical lines at x=300,350,400,450,500,550,600
for x in [300, 350, 400, 450, 500, 550, 600]:
    draw.line([(x, 100), (x, 310)], fill=(0, 0, 255, 80), width=1)
    draw.text((x, 90), f"x={x}", fill=(0, 0, 255))

img.save(OUTPUT)
print("Grid saved!")
