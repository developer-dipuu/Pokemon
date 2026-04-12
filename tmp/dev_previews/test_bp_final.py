from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).with_name("bp_final_preview.png")

img = Image.open(ROOT / 'assets' / 'battlepass.png').convert('RGBA')
draw = ImageDraw.Draw(img)

font_path = ROOT / 'assets' / 'Poppins-Bold.ttf'
medium_font_path = ROOT / 'assets' / 'Poppins-Medium.ttf'
tier_font = ImageFont.truetype(font_path, 14)
num_font = ImageFont.truetype(medium_font_path, 12)

# Simulate tier=12, progress=50 SP
current_tier = 12
progress = 50

# Tier number on badge
draw.text((352, 150), f"{min(current_tier, 25)}", font=tier_font, fill=(255, 255, 255), anchor="mm")

# Progress bar inside white capsule
pct = progress / 100.0
bar_width = int(155 * pct)
draw.rectangle([405, 143, 405 + bar_width, 157], fill=(130, 100, 200))

# Tier slot numbers (page 2: tiers 11-20)
page = 2
start_tier = (page - 1) * 10
start_x = 100
gap_x = 79.2
start_y = 413

for i in range(10):
    tier_num = start_tier + i + 1
    if tier_num > 25:
        break
    bx = start_x + (gap_x * i)
    draw.text((bx, start_y), str(tier_num), font=num_font, fill=(0, 0, 0), anchor="mm")

img.save(OUTPUT)
print("Done!")
