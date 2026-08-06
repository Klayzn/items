#!/usr/bin/env python3
"""Replace duplicated AVA fallback images with deterministic item-specific art."""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import re
import subprocess
import textwrap
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:
    raise SystemExit("Pillow is required: python -m pip install Pillow") from exc


DEFAULT_ITEMS = Path(r"C:\[ava_fbwl_bundle]\ava_cache\decoded\items.json")
DEFAULT_ACTIVE_ITEMS = Path(r"C:\Ava\resources\[fb]\ui_inventory\data\items.json")
FALLBACK_NAMES = (
    "box",
    "dollars",
    "torsos",
    "bread",
    "drug_blue",
    "medikit",
    "fish",
    "key1",
    "iron",
    "card_id",
    "phone",
    "gold",
    "repair_toolkit",
    "weapon_pistol",
    "ammo_pistol",
    "wct_clip_ex",
)
FONT_REGULAR = Path(r"C:\Windows\Fonts\segoeui.ttf")
FONT_SEMIBOLD = Path(r"C:\Windows\Fonts\seguisb.ttf")
SIZE = 256


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS)
    parser.add_argument("--active-items", type=Path, default=DEFAULT_ACTIVE_ITEMS)
    parser.add_argument("--commit", default="HEAD", help="Commit whose added/modified PNGs are eligible")
    parser.add_argument(
        "--all-duplicates",
        action="store_true",
        help="include every duplicated PNG represented in the AVA item definitions",
    )
    parser.add_argument(
        "--refresh-working-tree",
        action="store_true",
        help="re-render modified PNGs from their clean HEAD artwork",
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_source_items(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("items source must be an object")
    return value


def item_data(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    nested = entry.get("data")
    return nested if isinstance(nested, dict) else entry


def changed_pngs(repo: Path, commit: str) -> set[str]:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "-C",
            str(repo),
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "--diff-filter=AM",
            commit,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return {
        Path(line.strip()).name
        for line in result.stdout.splitlines()
        if line.strip().lower().endswith(".png")
    }


def working_tree_pngs(repo: Path) -> set[str]:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "-C",
            str(repo),
            "diff",
            "HEAD",
            "--name-only",
            "--diff-filter=AM",
            "--",
            "*.png",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return {Path(line.strip()).name for line in result.stdout.splitlines() if line.strip()}


def git_content(repo: Path, name: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{name}"],
        check=True,
        capture_output=True,
    )
    return result.stdout


def accent_for(item_name: str) -> tuple[int, int, int]:
    raw = hashlib.sha256(item_name.encode("utf-8")).digest()
    hue = raw[0] / 255
    saturation = 0.52 + raw[1] / 255 * 0.22
    lightness = 0.48 + raw[2] / 255 * 0.12
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return round(red * 255), round(green * 255), round(blue * 255)


def font(path: Path, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default()


def clean_label(item_name: str, entry: Any) -> str:
    data = item_data(entry)
    value = data.get("formatname") or data.get("label") or item_name
    label = re.sub(r"~[A-Za-z0-9_]+~", "", str(value)).strip()
    return label or item_name


def short_code(label: str, item_name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", label)
    if len(words) >= 2:
        return "".join(word[0] for word in words[:3]).upper()
    source = words[0] if words else item_name
    return source[:3].upper()


def fitted_label(draw: ImageDraw.ImageDraw, label: str) -> tuple[str, ImageFont.ImageFont]:
    words = label.split()
    lines = textwrap.wrap(label, width=19, break_long_words=True, break_on_hyphens=True) or [label]
    if len(lines) > 2:
        lines = [lines[0], " ".join(lines[1:])]
    lines = lines[:2]
    if len(lines[-1]) > 20:
        lines[-1] = lines[-1][:19].rstrip() + "..."
    text = "\n".join(lines)
    for size in range(24, 13, -1):
        current = font(FONT_SEMIBOLD, size)
        box = draw.multiline_textbbox((0, 0), text, font=current, spacing=2, align="center")
        if box[2] - box[0] <= 218 and box[3] - box[1] <= 52:
            return text, current
    return text, font(FONT_SEMIBOLD, 14)


def semantic_family(item_name: str, entry: Any) -> str:
    lowered = item_name.lower()
    item_type = str(item_data(entry).get("type") or "").lower()
    if "seed" in lowered or "graine" in lowered:
        return "seed"
    if lowered.startswith(("lg_card_", "card_", "carte", "id_")) or item_type in {"paper", "visitcards"}:
        return "card"
    if lowered.startswith(("wct_", "wt_")) or item_type == "weapon_component":
        return "component"
    if lowered.startswith("ammo_") or item_type == "weapon_ammo":
        return "ammo"
    if lowered.startswith("weapon_") or item_type == "weapon":
        return "weapon"
    if lowered.startswith(("phone", "pc_", "drone")) or item_type == "phones":
        return "device"
    if item_type in {"drug", "ingredients", "health"}:
        return "chemical"
    if item_type in {"consumable", "consumable_special", "food_ingredient"}:
        return "food"
    if item_type in {"tools", "props"}:
        return "tool"
    return "package"


def draw_seed(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    draw.rounded_rectangle((61, 45, 195, 182), radius=15, fill=(36, 35, 38, 245), outline=accent, width=5)
    draw.polygon(((76, 49), (180, 49), (190, 72), (66, 72)), fill=(*accent, 210))
    draw.ellipse((111, 93, 143, 127), fill=(180, 134, 72, 255))
    draw.line((128, 96, 128, 76), fill=(120, 205, 112, 255), width=6)
    draw.ellipse((104, 72, 128, 88), fill=(91, 186, 92, 255))
    draw.ellipse((128, 68, 152, 85), fill=(111, 205, 104, 255))


def draw_card(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int], code: str) -> None:
    draw.rounded_rectangle((47, 28, 209, 190), radius=18, fill=(30, 30, 33, 250), outline=accent, width=6)
    draw.rounded_rectangle((61, 43, 195, 74), radius=8, fill=(*accent, 220))
    draw.ellipse((84, 91, 132, 139), fill=(225, 225, 220, 235))
    draw.rounded_rectangle((75, 142, 181, 158), radius=7, fill=(115, 115, 120, 210))
    draw.text((178, 174), code[:2], anchor="mm", font=font(FONT_SEMIBOLD, 21), fill=accent)


def draw_component(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    draw.rounded_rectangle((44, 73, 212, 147), radius=18, fill=(34, 35, 39, 250), outline=accent, width=5)
    draw.rectangle((66, 92, 169, 127), fill=(79, 82, 89, 255))
    draw.rectangle((169, 101, 225, 120), fill=(*accent, 235))
    for offset in range(76, 157, 20):
        draw.line((offset, 94, offset, 125), fill=(175, 176, 181, 180), width=3)


def draw_ammo(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    draw.rounded_rectangle((48, 88, 208, 174), radius=12, fill=(48, 50, 43, 255), outline=accent, width=5)
    draw.rectangle((48, 91, 208, 111), fill=(*accent, 205))
    for offset in (78, 106, 134, 162):
        draw.rounded_rectangle((offset, 49, offset + 13, 103), radius=5, fill=(202, 165, 74, 255))
        draw.polygon(((offset, 49), (offset + 6, 36), (offset + 13, 49)), fill=(225, 194, 108, 255))


def draw_weapon(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int], item_name: str) -> None:
    lowered = item_name.lower()
    if any(token in lowered for token in ("bag", "luggage", "toolbox")):
        draw.rounded_rectangle((55, 77, 201, 178), radius=20, fill=(47, 49, 55, 255), outline=accent, width=6)
        draw.arc((91, 40, 165, 105), 180, 360, fill=accent, width=9)
        draw.rectangle((77, 111, 179, 126), fill=(*accent, 210))
    elif any(token in lowered for token in ("bat", "pipe", "shovel", "knife", "katana", "stunrod")):
        draw.line((61, 166, 190, 54), fill=(205, 208, 214, 255), width=18)
        draw.line((50, 183, 91, 148), fill=accent, width=25)
        draw.polygon(((177, 66), (218, 28), (194, 82)), fill=(230, 232, 236, 255))
    elif any(token in lowered for token in ("bomb", "fall", "pressure")):
        draw.ellipse((67, 67, 189, 189), fill=(46, 48, 51, 255), outline=accent, width=6)
        draw.arc((126, 28, 207, 100), 185, 284, fill=(220, 178, 75, 255), width=8)
        draw.ellipse((187, 29, 205, 47), fill=(255, 104, 67, 255))
    else:
        draw.polygon(((38, 89), (183, 73), (218, 99), (176, 119), (133, 119), (118, 170), (83, 170), (91, 119), (38, 119)), fill=(55, 58, 64, 255))
        draw.line((51, 101, 194, 91), fill=accent, width=7)


def draw_device(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int], item_name: str) -> None:
    if "drone" in item_name.lower():
        draw.ellipse((110, 93, 146, 129), fill=(*accent, 255))
        draw.line((61, 77, 195, 145), fill=(156, 159, 166, 255), width=8)
        draw.line((61, 145, 195, 77), fill=(156, 159, 166, 255), width=8)
        for x, y in ((57, 73), (199, 73), (57, 149), (199, 149)):
            draw.ellipse((x - 22, y - 9, x + 22, y + 9), outline=accent, width=5)
    else:
        draw.rounded_rectangle((82, 34, 174, 190), radius=18, fill=(38, 40, 45, 255), outline=accent, width=6)
        draw.rounded_rectangle((94, 55, 162, 151), radius=7, fill=(*accent, 130))
        draw.ellipse((122, 165, 134, 177), fill=(205, 207, 212, 255))


def draw_chemical(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    draw.rectangle((109, 39, 147, 85), fill=(211, 213, 218, 255))
    draw.rounded_rectangle((79, 77, 177, 183), radius=24, fill=(44, 47, 52, 255), outline=(211, 213, 218, 255), width=5)
    draw.polygon(((85, 134), (171, 117), (171, 174), (85, 174)), fill=(*accent, 220))
    draw.ellipse((102, 113, 119, 130), fill=(255, 255, 255, 150))


def draw_food(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    draw.ellipse((51, 70, 205, 185), fill=(44, 45, 48, 255), outline=accent, width=6)
    draw.ellipse((66, 78, 190, 145), fill=(*accent, 210))
    draw.arc((66, 103, 190, 187), 0, 180, fill=(225, 226, 221, 235), width=7)
    draw.line((170, 50, 122, 127), fill=(205, 207, 211, 255), width=8)


def draw_tool(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    draw.line((66, 177, 184, 59), fill=accent, width=18)
    draw.ellipse((157, 37, 210, 90), outline=(208, 210, 216, 255), width=13)
    draw.line((58, 50, 190, 181), fill=(164, 167, 174, 255), width=13)
    draw.rectangle((47, 36, 72, 76), fill=(164, 167, 174, 255))


def draw_package(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    draw.polygon(((61, 69), (128, 34), (195, 69), (128, 104)), fill=(*accent, 230))
    draw.polygon(((61, 69), (128, 104), (128, 188), (61, 151)), fill=(47, 48, 53, 255))
    draw.polygon(((195, 69), (128, 104), (128, 188), (195, 151)), fill=(67, 69, 76, 255))
    draw.line((128, 104, 128, 188), fill=accent, width=5)


def render_semantic(item_name: str, entry: Any, label: str) -> Image.Image:
    accent = accent_for(item_name)
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((32, 177, 224, 213), fill=(0, 0, 0, 70))
    family = semantic_family(item_name, entry)
    code = short_code(label, item_name)
    {
        "seed": lambda: draw_seed(draw, accent),
        "card": lambda: draw_card(draw, accent, code),
        "component": lambda: draw_component(draw, accent),
        "ammo": lambda: draw_ammo(draw, accent),
        "weapon": lambda: draw_weapon(draw, accent, item_name),
        "device": lambda: draw_device(draw, accent, item_name),
        "chemical": lambda: draw_chemical(draw, accent),
        "food": lambda: draw_food(draw, accent),
        "tool": lambda: draw_tool(draw, accent),
        "package": lambda: draw_package(draw, accent),
    }[family]()
    draw.rounded_rectangle((90, 91, 166, 142), radius=12, fill=(13, 13, 15, 210), outline=(*accent, 220), width=3)
    draw.text((128, 116), code, anchor="mm", font=font(FONT_SEMIBOLD, 30), fill=(245, 245, 242, 255))
    add_label(draw, label, accent)
    return image


def add_label(draw: ImageDraw.ImageDraw, label: str, accent: tuple[int, int, int]) -> None:
    draw.rounded_rectangle((14, 190, 242, 248), radius=13, fill=(12, 12, 14, 228), outline=(*accent, 210), width=2)
    text, text_font = fitted_label(draw, label)
    box = draw.multiline_textbbox((0, 0), text, font=text_font, spacing=2, align="center")
    position = (128 - (box[0] + box[2]) / 2, 219 - (box[1] + box[3]) / 2)
    draw.multiline_text(position, text, font=text_font, spacing=2, align="center", fill=(246, 245, 241, 255))


def render_variant(source: Path | bytes, item_name: str, label: str) -> Image.Image:
    accent = accent_for(item_name)
    original = Image.open(BytesIO(source) if isinstance(source, bytes) else source).convert("RGBA")
    original.thumbnail((224, 182), Image.Resampling.LANCZOS)
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    image.alpha_composite(original, ((SIZE - original.width) // 2, max(4, (184 - original.height) // 2)))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rounded_rectangle((8, 8, 248, 248), radius=24, outline=(*accent, 195), width=5)
    code = short_code(label, item_name)
    draw.ellipse((188, 14, 244, 70), fill=(13, 13, 15, 230), outline=(*accent, 230), width=3)
    draw.text((216, 42), code[:3], anchor="mm", font=font(FONT_SEMIBOLD, 20), fill=(247, 247, 244, 255))
    add_label(draw, label, accent)
    return image


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    source_items = load_source_items(args.items)
    if args.active_items.exists():
        for item_name, entry in load_source_items(args.active_items).items():
            source_items.setdefault(item_name, entry)
    if args.refresh_working_tree:
        candidates = working_tree_pngs(repo)
        fallback_hashes = {
            content_hash(git_content(repo, f"{name}.png"))
            for name in FALLBACK_NAMES
            if (repo / f"{name}.png").exists()
        }
        targets: list[tuple[Path, bool, Path | bytes]] = []
        for name in candidates:
            image = repo / name
            if image.stem not in source_items:
                continue
            source = git_content(repo, name)
            targets.append((image, content_hash(source) in fallback_hashes, source))
        targets.sort(key=lambda row: row[0].name.casefold())
        return render_targets(targets, source_items, args.apply)

    candidates = (
        {path.name for path in repo.glob("*.png")}
        if args.all_duplicates
        else changed_pngs(repo, args.commit)
    )
    images = list(repo.glob("*.png"))
    groups: dict[str, list[Path]] = defaultdict(list)
    for image in images:
        groups[file_hash(image)].append(image)

    fallback_hashes = {
        file_hash(path)
        for name in FALLBACK_NAMES
        if (path := repo / f"{name}.png").exists()
    }
    targets: list[tuple[Path, bool, Path | bytes]] = []
    for digest, members in groups.items():
        if len(members) < 2:
            continue
        semantic = digest in fallback_hashes
        for image in members:
            if image.name not in candidates:
                continue
            item_name = image.stem
            if item_name not in source_items:
                continue
            targets.append((image, semantic, image))

    targets.sort(key=lambda row: row[0].name.casefold())

    return render_targets(targets, source_items, args.apply)


def render_targets(
    targets: list[tuple[Path, bool, Path | bytes]], source_items: dict[str, Any], apply: bool
) -> int:
    semantic_count = sum(1 for _, semantic, _ in targets if semantic)
    variant_count = len(targets) - semantic_count
    print(f"eligible duplicated images: {len(targets)}")
    print(f"semantic replacements: {semantic_count}")
    print(f"source-variant labels: {variant_count}")
    if not apply:
        for image, semantic, _ in targets:
            print(f"  {'semantic' if semantic else 'variant'}: {image.name}")
        return 0

    for image, semantic, source in targets:
        item_name = image.stem
        entry = source_items[item_name]
        label = clean_label(item_name, entry)
        rendered = render_semantic(item_name, entry, label) if semantic else render_variant(source, item_name, label)
        rendered.save(image, format="PNG", optimize=True)

    post_hashes = [file_hash(image) for image, _, _ in targets]
    print(f"rendered: {len(targets)}")
    print(f"unique rendered hashes: {len(set(post_hashes))}")
    return 0 if len(post_hashes) == len(set(post_hashes)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
