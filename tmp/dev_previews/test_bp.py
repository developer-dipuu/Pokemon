from PIL import Image, ImageDraw, ImageFont
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).with_name("bp_test_output.png")

def draw_battlepass(page: int, current_tier: int, current_sp: int):
    # current_tier is e.g. 1-25. 1-indexed.
    # page is 1, 2, or 3 (1-10, 11-20, 21-30)
    img = Image.open(ROOT / "assets" / "battlepass.png").convert("RGBA")
    draw = ImageDraw.Draw(img)
    
    # Text bounds:
    # "in each tier box below we have small box there we hae to edit the number"
    # Small boxes are below the 10 square slots. Let's find coordinates.
    # Image width: 899. 10 boxes ~ 80px each.
    # I'll guess start_x = 75, gap = 81.
    start_x = 75
    start_y = 420
    gap = 81
    
    # Progress bar "near that progress bar lft side we add current tier number"
    # The progress bar is from the top, likely under "BATTLE PASS".
    # I'll guess it's at y=140, x=350 to 600
    draw.text((370, 140), f"Tier {current_tier}", fill="white")
    if current_tier <= 25:
        # draw progress
        pct = current_sp / 100
        bar_width = min(200, int(200 * pct))
        draw.rectangle([480, 145, 480 + bar_width, 155], fill="white")
    
    # Draw numbers
    start_tier = (page - 1) * 10 + 1
    for i in range(10):
        tier_num = start_tier + i
        if tier_num > 25:
            break
        bx = start_x + (gap * i)
        by = start_y
        draw.text((bx, by), str(tier_num), fill="white")
    
    img.save(OUTPUT)

draw_battlepass(1, 12, 50)
