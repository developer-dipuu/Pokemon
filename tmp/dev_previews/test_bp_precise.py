from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).with_name("bp_precise_grid.png")

img = Image.open(ROOT / 'assets' / 'battlepass.png').convert('RGBA')
draw = ImageDraw.Draw(img)
font = ImageFont.load_default()

# Finer grid focused on the badge/progress bar area
# Badge is around x=315-385, y=130-170
# Progress bar white track is around x=390-575, y=137-162

# Draw precise markers every 5px in the badge+bar region
for x in range(310, 590, 5):
    for y in range(125, 175, 5):
        draw.point((x, y), fill=(255, 0, 0))

# Precise labels
for x in [315, 330, 345, 360, 375, 390, 400, 410, 420, 430, 440, 450, 460, 470, 480, 500, 520, 540, 560, 580]:
    draw.line([(x, 120), (x, 180)], fill=(0, 255, 0), width=1)
    draw.text((x-5, 112), str(x), fill=(0, 200, 0), font=font)

for y in [125, 130, 135, 140, 145, 150, 155, 160, 165, 170, 175]:
    draw.line([(305, y), (590, y)], fill=(255, 165, 0), width=1)
    draw.text((305, y-6), str(y), fill=(200, 100, 0), font=font)

img.save(OUTPUT)
print("Saved bp_precise_grid.png")
