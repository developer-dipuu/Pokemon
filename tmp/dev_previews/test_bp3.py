from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).with_name("bp_test_output3.png")

img = Image.open(ROOT / 'assets' / 'battlepass.png').convert('RGBA')
draw = ImageDraw.Draw(img)
font = ImageFont.load_default()

sx = 97
gap = 79.5
sy = 422 # approximate y for parallelogram center

for i in range(10):
    val = str(i + 1)
    bx = sx + (gap * i)
    draw.text((bx, sy), val, fill='white', font=font, anchor="mm")
    draw.point((bx, sy), fill='red')

# Current tier under time remaining
draw.text((433, 275), "1", fill='white', font=font, anchor="mm")

img.save(OUTPUT)
