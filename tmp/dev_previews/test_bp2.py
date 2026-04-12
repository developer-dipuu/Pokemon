from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).with_name("bp_test_output2.png")

# Image size: 899 x 503
# Bounding for 10 boxes visually observed from previous runs: 
# leftmost edge of box 1 is ~ 65, rightmost edge of box 10 is ~ 855
# Wait, let's draw text at multiple positions to find the perfect fit.

img = Image.open(ROOT / 'assets' / 'battlepass.png').convert('RGBA')
draw = ImageDraw.Draw(img)
font = ImageFont.load_default()

# Tiers 1-10 inside the parallelograms
# Let's test start_x from 80 to 110, gap = 77 to 82.
# We'll put small colored dots and text.

# Test 1: red
sx1 = 92
gap1 = 78.5
sy = 450
for i in range(10):
    val = str(i + 1)
    x = sx1 + gap1 * i
    draw.text((x, sy), val, fill='red', font=font)
    draw.point((x, sy), fill='white')

# Test 2: blue
sx2 = 98
gap2 = 80
sy2 = 455
for i in range(10):
    val = str(i + 1)
    x = sx2 + gap2 * i
    draw.text((x, sy2), val, fill='blue', font=font)
    draw.point((x, sy2), fill='white')

# Test 3: black
sx3 = 85
gap3 = 79
sy3 = 460
for i in range(10):
    val = str(i + 1)
    x = sx3 + gap3 * i
    draw.text((x, sy3), val, fill='black', font=font)
    draw.point((x, sy3), fill='white')

# "area below time remaining"
# Let's test a few places for the current tier
# "Time remaining" is around y=190.
draw.text((370, 240), 'A (370,240)', fill='black')
draw.text((410, 260), 'B (410,260)', fill='black')
draw.text((430, 265), 'C (430,265)', fill='black')
draw.text((450, 275), 'D (450,275)', fill='black')
draw.text((350, 265), 'E (350,265)', fill='black')
draw.text((394, 280), 'F (394,280)', fill='black')
draw.text((415, 280), 'G (415,280)', fill='black')
draw.text((390, 290), 'H (390,290)', fill='black')
draw.text((420, 290), 'I (420,290)', fill='black')

img.save(OUTPUT)
