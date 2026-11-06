#!/usr/bin/env python3
"""Build the branded 1600x900 images used by the November 2026 campaign.

Needs Pillow (not a bot dependency): run with a system Python that has it.
Sources are the original EN app screenshots; screens that show the
"Booking is final" notice, a medical-certificate line or a gym name that
must not appear are cropped before they are used.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SCREENS = Path(
    "/Users/floriano/1.Floriano/flexdropin_doc/flexdropin_app_img/Flexdropin-compressed"
)
LOGO = Path("/Users/floriano/flo_mobile_app/remotion/public/brand/logo.png")
OUT = Path(os.getenv("NOVEMBER_ASSETS_OUT", Path(__file__).resolve().parents[1] / "media_library" / "november-2026"))
FONT = "/System/Library/Fonts/Avenir Next.ttc"
W, H = 1600, 900
LIME, WHITE, SOFT = (142, 224, 74), (242, 245, 243), (201, 211, 206)
BG_TOP, BG_BOTTOM = (8, 32, 25), (12, 43, 33)


def font(size: int, weight: str = "heavy") -> ImageFont.FreeTypeFont:
    index = {"heavy": 8, "bold": 0, "medium": 5, "regular": 7}[weight]
    return ImageFont.truetype(FONT, size, index=index)


def background() -> Image.Image:
    image = Image.new("RGB", (W, H), BG_TOP)
    pixels = ImageDraw.Draw(image)
    for y in range(H):
        ratio = y / (H - 1)
        pixels.line(
            [(0, y), (W, y)],
            fill=tuple(round(a + (b - a) * ratio) for a, b in zip(BG_TOP, BG_BOTTOM)),
        )
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse((W - 760, -380, W + 260, 560), fill=(31, 192, 122))
    glow = glow.filter(ImageFilter.GaussianBlur(190))
    return Image.blend(image, Image.composite(glow, image, glow.convert("L")), 0.28)


def wrap(draw, text, fnt, width):
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=fnt) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_headline(image, x, y, lines, width, size, accent=()):
    """Draw headline lines; words listed in ``accent`` are coloured lime."""
    draw = ImageDraw.Draw(image)
    fnt = font(size)
    for line in lines:
        cursor = x
        for word in line.split():
            colour = LIME if word.strip(".,?!").lower() in accent else WHITE
            draw.text((cursor, y), word, font=fnt, fill=colour)
            cursor += draw.textlength(word + " ", font=fnt)
        y += int(size * 1.12)
    return y


def add_logo(image, x=96, y=H - 150, height=70):
    logo = Image.open(LOGO).convert("RGBA")
    logo = logo.resize((height, height))
    image.paste(logo, (x, y), logo)
    ImageDraw.Draw(image).text(
        (x + height + 18, y + 14), "FlexDropin", font=font(34, "bold"), fill=WHITE,
    )


def rounded(image, radius):
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *image.size), radius, fill=255)
    out = image.convert("RGBA")
    out.putalpha(mask)
    return out


def phone(name, crop=(0.0, 1.0), height=760):
    """Return a screenshot cropped vertically by fractions of its height."""
    shot = Image.open(SCREENS / f"{name}.png").convert("RGB")
    top, bottom = crop
    shot = shot.crop((0, int(shot.height * top), shot.width, int(shot.height * bottom)))
    width = round(shot.width * height / shot.height)
    shot = shot.resize((width, height), Image.LANCZOS)
    framed = Image.new("RGBA", (width + 24, height + 24), (255, 255, 255, 235))
    framed = rounded(framed, 52)
    framed.paste(rounded(shot, 42), (12, 12), rounded(shot, 42))
    return framed


def screen_card(file, headline, accent, sub, screen, crop=(0.0, 1.0), height=740):
    image = background()
    shot = phone(screen, crop, height)
    px = W - shot.width - 120
    py = (H - shot.height) // 2
    text_width = px - 96 - 50
    probe = ImageDraw.Draw(image)
    size = 88
    while size > 56:
        lines = wrap(probe, headline, font(size), text_width)
        if len(lines) <= 4 and all(
            probe.textlength(line, font=font(size)) <= text_width for line in lines
        ):
            break
        size -= 4
    sub_lines = wrap(probe, sub, font(36, "medium"), text_width)
    block = int(size * 1.12) * len(lines) + 30 + 48 * len(sub_lines)
    y = (H - block) // 2 - 40
    y = draw_headline(image, 96, y, lines, text_width, size, accent)
    for line in sub_lines:
        probe.text((96, y + 30), line, font=font(36, "medium"), fill=SOFT)
        y += 48
    shadow = Image.new("RGBA", (shot.width + 120, shot.height + 120), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (60, 80, 60 + shot.width, 80 + shot.height), 52, fill=(0, 0, 0, 150),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(34))
    image.paste(shadow, (px - 60, py - 60), shadow)
    image.paste(shot, (px, py), shot)
    add_logo(image)
    save(image, file)


def text_card(file, kicker, headline, accent, sub):
    image = background()
    draw = ImageDraw.Draw(image)
    lines = wrap(draw, headline, font(112), W - 192)
    sub_lines = wrap(draw, sub, font(44, "medium"), W - 260)
    block = 60 + int(112 * 1.12) * len(lines) + 44 + 60 * len(sub_lines)
    y = (H - block) // 2 - 30
    draw.text((96, y), kicker.upper(), font=font(34, "bold"), fill=LIME)
    y = draw_headline(image, 96, y + 70, lines, W - 192, 112, accent)
    for line in sub_lines:
        draw.text((96, y + 36), line, font=font(44, "medium"), fill=SOFT)
        y += 60
    add_logo(image)
    save(image, file)


def save(image, name):
    OUT.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(OUT / name, optimize=True)
    print("wrote", OUT / name)


def main():
    screen_card(
        "screen-explore.png", "Three gyms. One map. No contract.", {"contract"},
        "Open FlexDropin in a new place and see what is nearby today.",
        "Explore", crop=(0.0, 0.8),
    )
    screen_card(
        "screen-class-list.png", "Time, price and spots left. All in one list.", {"spots", "left"},
        "Pick the class that fits your day.",
        "Class list", crop=(0.0, 0.72),
    )
    screen_card(
        "screen-bookings.png", "Every booking in one place.", {"booking", "place"},
        "Today, upcoming and past, with the address and directions a tap away.",
        "Bookings", crop=(0.0, 0.72),
    )
    screen_card(
        "screen-payment.png", "Pay in the app. Then just show up.", {"pay"},
        "Apple Pay or card, right inside the booking.",
        "Payment", crop=(0.38, 1.0), height=700,
    )
    screen_card(
        "screen-gym-detail.png", "Read the reviews before you walk in.", {"reviews"},
        "Disciplines, rating and distance sit on one gym page.",
        "Gym detail", crop=(0.0, 0.5),
    )
    screen_card(
        "screen-widget.png", "Your next class, on your Home Screen.", {"next", "class"},
        "The widget shows the gym, the start time and a countdown.",
        "Widget", crop=(0.0, 0.52),
    )
    cards = {
        "card-crossfit.png": ("First class", "Your first CrossFit class.", {"crossfit"}, "Arrive early. Mention injuries. Scale without apology."),
        "card-yoga.png": ("Class etiquette", "Yoga etiquette in one line.", {"yoga"}, "Take the pose you need, not the pose the room is doing."),
        "card-boxing.png": ("First class", "A first boxing class.", {"boxing"}, "Footwork, shadowboxing, bag work and breathing."),
        "card-pilates.png": ("Good to know", "Pilates is not a gentle class.", {"pilates"}, "The slow tempo is the workout."),
        "card-hyrox.png": ("Explained", "HYROX in one sentence.", {"hyrox"}, "Running plus eight functional stations, repeated."),
        "card-calisthenics.png": ("Strength anywhere", "Calisthenics rewards patience.", {"calisthenics"}, "Pull-ups, dips, push-ups and slow progress."),
        "card-lifting.png": ("Technique first", "Learning Olympic lifting?", {"lifting"}, "Expect a lot of empty-bar work first."),
        "card-cycling.png": ("Find your rhythm", "Indoor cycling is about cadence.", {"cadence"}, "Finish the ride first. Add resistance when you're ready."),
    }
    for file, (kicker, headline, accent, sub) in cards.items():
        text_card(file, kicker, headline, accent, sub)


if __name__ == "__main__":
    main()
