#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OGP画像生成スクリプト (Phase 2)
--------------------------------
data/scenes.json の21種類のSVGシーンをもとに、1200x630のOGP画像を
assets/ogp/{scene}.png として生成する。

設計方針:
- 記事ごとに個別のOGP画像は作らない(3,000記事規模になっても画像生成コストが
  増えないようにするため)。記事は21種類のsceneのいずれかを持つので、
  シーンごとに1枚(=21枚)生成すれば全記事をカバーできる。
- 新しいsceneを追加しない限り、このスクリプトを再実行する必要はない
  (日次ビルドのたびに実行する必要はない)。

実行方法:
    python3 generate_ogp_images.py
"""
import json
import os
import re

import cairosvg
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "assets", "ogp")
OGP_W, OGP_H = 1200, 630

os.makedirs(OUT_DIR, exist_ok=True)


def load_json(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return json.load(f)


def find_font(candidates, size):
    for path in candidates:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# CJK対応フォントを探す(無ければPillowのデフォルトにフォールバック)
CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
]


def render_scene_to_png(svg_markup, width, height):
    """SVG(viewBox 0 0 300 120, preserveAspectRatio=noneのstretch前提)を
    OGPのアスペクト比(1200x630)向けに、中央基準でクロップして描画する。"""
    # 元のviewBoxを維持しつつ preserveAspectRatio=xMidYMid slice に差し替えて、
    # 引き伸ばしではなく「カバー(はみ出た分をクロップ)」で描画させる
    svg_for_ogp = re.sub(
        r'preserveAspectRatio="none"',
        'preserveAspectRatio="xMidYMid slice"',
        svg_markup,
    )
    png_bytes = cairosvg.svg2png(
        bytestring=svg_for_ogp.encode("utf-8"),
        output_width=width,
        output_height=height,
    )
    import io
    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def add_brand_band(img):
    """画像下部に湘南Doorsのブランドバンド(グラデーション+ロゴテキスト)を重ねる。"""
    img = img.convert("RGBA")
    band_h = 170
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # 下から上へ徐々に透明になるグラデーションの帯
    for y in range(band_h):
        alpha = int(200 * (y / band_h))
        draw.line([(0, img.height - band_h + y), (img.width, img.height - band_h + y)],
                   fill=(15, 20, 30, alpha))
    draw.rectangle([0, img.height - 40, img.width, img.height], fill=(15, 20, 30, 235))

    brand_font = find_font(CJK_FONT_CANDIDATES, 44)
    tagline_font = find_font(CJK_FONT_CANDIDATES, 22)
    draw.text((48, img.height - 118), "湘南Doors", font=brand_font, fill=(255, 255, 255, 255))
    draw.text((48, img.height - 60), "SHONAN DOORS — 湘南と、人をつなぐ地域メディア",
              font=tagline_font, fill=(230, 230, 225, 230))

    return Image.alpha_composite(img, overlay).convert("RGB")


def main():
    scenes = load_json("data/scenes.json")
    for name, svg_markup in scenes.items():
        img = render_scene_to_png(svg_markup, OGP_W, OGP_H)
        img = add_brand_band(img)
        out_path = os.path.join(OUT_DIR, f"{name}.png")
        img.save(out_path, "PNG", optimize=True)
    print(f"OGP画像を{len(scenes)}枚生成しました: {OUT_DIR}")


if __name__ == "__main__":
    main()
