"""Apps Store cover image (1200x600): icon, name, tagline and a screenshot.

    python make_banner.py <icon.png> <screenshot.png> <out.png>
Uses DejaVu Sans (free license), available in the Odoo image.
"""
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

icon_path, shot_path, out_path = sys.argv[1:4]
W, H = 1200, 600
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

banner = Image.new('RGB', (W, H))
pixels = banner.load()
start, end = (63, 52, 160), (0, 120, 150)
for y in range(H):
    for x in range(W):
        t = (x / W) * 0.7 + (y / H) * 0.3
        pixels[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(start, end))

draw = ImageDraw.Draw(banner)
icon = Image.open(icon_path).convert('RGBA').resize((104, 104), Image.LANCZOS)
banner.paste(icon, (64, 96), icon)
draw.text((64, 228), "Unofficial", font=ImageFont.truetype(FONT, 34), fill=(225, 225, 255))
draw.text((64, 268), "Telegram Chat", font=ImageFont.truetype(BOLD, 48), fill='white')
small = ImageFont.truetype(FONT, 19)
for index, line in enumerate([
    "Your personal Telegram inside Odoo",
    "Private per user  ·  QR login  ·  No extra server",
]):
    draw.text((64, 352 + index * 36), line, font=small, fill=(230, 235, 255))
draw.text((64, 532), "Not affiliated with Telegram. Uses the Telegram API.",
          font=ImageFont.truetype(FONT, 15), fill=(200, 205, 240))

# Screenshot in a soft-shadowed frame on the right.
shot = Image.open(shot_path).convert('RGB')
shot = shot.crop((0, 46, shot.width, shot.height))  # drop Odoo's top bar
target_w = 560
shot = shot.resize((target_w, round(shot.height * target_w / shot.width)), Image.LANCZOS)
shot = shot.crop((0, 0, target_w, min(shot.height, 420)))
x, y = W - target_w - 48, (H - shot.height) // 2
shadow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle((x - 6, y - 2, x + target_w + 10, y + shot.height + 14), 18, fill=(0, 0, 0, 110))
banner = Image.alpha_composite(banner.convert('RGBA'), shadow.filter(ImageFilter.GaussianBlur(12)))
mask = Image.new('L', shot.size, 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, shot.width - 1, shot.height - 1), 12, fill=255)
banner.paste(shot, (x, y), mask)
banner.convert('RGB').save(out_path, optimize=True)
print("banner written", banner.size)
