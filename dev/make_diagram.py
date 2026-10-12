""""How it works" diagram for the store page and README (1200x520 PNG).

    python make_diagram.py <out.png>
Uses DejaVu Sans (free license), available in the Odoo image.
"""
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 520
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
title_font, head, body, small = (ImageFont.truetype(BOLD, 24), ImageFont.truetype(BOLD, 18),
                                 ImageFont.truetype(FONT, 15), ImageFont.truetype(FONT, 13))
PURPLE, TEAL, GREY, INK = (113, 99, 186), (0, 140, 160), (238, 236, 246), (40, 40, 60)

image = Image.new('RGB', (W, H), 'white')
draw = ImageDraw.Draw(image)
draw.text((40, 24), "How it works", font=title_font, fill=INK)
draw.text((40, 58), "Everything runs inside your Odoo. Nothing else to install.", font=body, fill=(90, 90, 110))


def box(x, y, w, h, title, lines, color):
    draw.rounded_rectangle((x, y, x + w, y + h), 14, fill=GREY, outline=color, width=3)
    draw.text((x + 18, y + 14), title, font=head, fill=color)
    for index, line in enumerate(lines):
        draw.text((x + 18, y + 46 + index * 22), line, font=body, fill=INK)


def arrow(x1, y1, x2, y2, label, color=INK, both=False):
    draw.line((x1, y1, x2, y2), fill=color, width=3)
    for (xa, ya, xb, yb) in ([(x1, y1, x2, y2)] + ([(x2, y2, x1, y1)] if both else [])):
        direction = 1 if xb > xa else -1
        draw.polygon([(xb, yb), (xb - 12 * direction, yb - 7), (xb - 12 * direction, yb + 7)], fill=color)
    draw.text(((x1 + x2) // 2 - draw.textlength(label, font=small) // 2, y1 - 24), label, font=small, fill=(90, 90, 110))


# Odoo server frame
draw.rounded_rectangle((300, 100, 895, 480), 18, outline=PURPLE, width=2)
draw.text((318, 108), "Your Odoo server", font=head, fill=PURPLE)

box(40, 150, 220, 170, "Each user", ["Odoo in the browser:", "chat list, messages,", "login with QR code"], PURPLE)
box(330, 150, 250, 130, "Odoo web requests", ["check permissions,", "queue the work"], PURPLE)
box(330, 320, 250, 130, "Your database", ["chats and messages,", "visible to their owner;", "sessions encrypted"], PURPLE)
box(645, 150, 230, 170, "Telegram engine", ["scheduled action;", "one connection per", "account, kept open"], TEAL)
box(950, 150, 215, 170, "Telegram", ["official servers", "(MTProto, encrypted)", "user's own account"], TEAL)

arrow(260, 215, 330, 215, "clicks", PURPLE)
arrow(580, 215, 645, 215, "queue", PURPLE)
arrow(875, 215, 950, 215, "API", TEAL, both=True)
draw.line((760, 320, 760, 385, 580, 385), fill=TEAL, width=3)
draw.polygon([(580, 385), (592, 378), (592, 392)], fill=TEAL)
draw.text((600, 395), "saves new messages", font=small, fill=(90, 90, 110))
draw.line((150, 320, 150, 420, 330, 420), fill=PURPLE, width=3)
draw.polygon([(150, 320), (143, 332), (157, 332)], fill=PURPLE)
draw.text((160, 428), "live updates (Odoo bus)", font=small, fill=(90, 90, 110))

image.save(sys.argv[1], optimize=True)
print("diagram written", image.size)
