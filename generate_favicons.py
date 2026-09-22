"""
generate_favicons.py
----------------------
湘南Doors公式ロゴから、favicon専用に最適化した画像一式を生成する。
generate_ogp_images.py / generate_scenic_images.py と同じ位置づけの
「手動/一回限り実行するアセット生成スクリプト」であり、build.pyの
日次実行フローには含まれない。

【背景】
以前の実装は、ロゴ画像(白背景版)の上部をそのまま正方形に収めただけで、
- 背景が正方形いっぱいの白塗りで、四隅にも背景色が残る
- アイコンがフレームぎりぎりまで詰まっており余白がない
- 円形/角丸に見えず、単に「四角い画像を縮小しただけ」に見える
という、小サイズでのfavicon表示に適さない見た目になっていた。

【今回の方針】
- 元ロゴ(本体ブランドロゴ自体は変更しない)から、透過背景版
  (Shonan_Doors_Logo.png)のアイコン部分(扉・太陽・海・波。
  「Shonan Doors」の文字は小サイズで潰れるため含めない)だけを使用する。
- 正方形の透過キャンバス上に、既存サイトのブランドカラー
  (--trust: #1D3557、site.cssで定義済みの色をそのまま再利用)で
  塗った「円」を背景として配置する。
- その円の中に、十分な余白を持たせてアイコンを中央配置する。
- 円の外側(キャンバスの四隅)は完全に透過のままにする。
これにより、Instagram/ホットペッパー等と同様の
「ブランドカラーの円形アイコン、外側は透明」という
一般的なfaviconデザインパターンに合わせている。

入力:
  /mnt/user-data/uploads/Shonan_Doors_Logo.png
    (1254x1254, 透過背景, アイコン+「Shonan Doors」文字を含むロゴ全体)

出力(リポジトリ直下、すべて正方形):
  favicon.ico                  (16/32/48 のマルチサイズICO)
  favicon-16x16.png
  favicon-32x32.png
  favicon-48x48.png
  favicon-96x96.png
  apple-touch-icon.png         (180x180)
  android-chrome-192x192.png
  android-chrome-512x512.png
"""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE = "/mnt/user-data/uploads/Shonan_Doors_Logo.png"

# ロゴ全体(1254x1254)のうち、上から何pxまでが「アイコン部分」かの目安。
# それ以降は「Shonan Doors」の文字部分になるため、faviconには含めない。
ICON_ONLY_CROP_HEIGHT = 1000

# 高解像度の作業用マスターサイズ(ここから各サイズへダウンサンプルする)
MASTER_SIZE = 1024

# 既存サイトのブランドカラー(assets/site.css の --trust と同じ値)。
# ここでも同じ16進値を直接使う(CSSファイルをPythonから読み込む仕組みは
# 無いため値を複製しているが、色自体は既存定義からのコピー)。
BRAND_CIRCLE_COLOR = (0x1D, 0x35, 0x57, 255)  # #1D3557

# 円の直径 ÷ キャンバス全体の比率(四隅の透過マージンを十分に確保する)
CIRCLE_RATIO = 0.86
# アイコン(扉・太陽等)の幅 ÷ 円の直径の比率(円の中でも余白を持たせる)
ICON_RATIO_IN_CIRCLE = 0.62


def build_favicon_master():
    """円形ブランドカラー背景+中央にアイコンを配置した、
    高解像度(MASTER_SIZE四方)のfavicon用マスター画像(RGBA・四隅透過)を作る。"""
    src = Image.open(SOURCE).convert("RGBA")

    # アイコン部分(文字を含まない上部)を、実際の非透過ピクセルの
    # bounding boxで自動トリムする(元アートワークの余白を巻き込まない)。
    icon_area = src.crop((0, 0, src.width, ICON_ONLY_CROP_HEIGHT))
    bbox = icon_area.getbbox()
    icon = icon_area.crop(bbox)

    canvas = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))

    # ブランドカラーの円を描画(円の外側=キャンバス四隅は透過のまま)。
    circle_diameter = int(MASTER_SIZE * CIRCLE_RATIO)
    circle_layer = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(circle_layer)
    offset = (MASTER_SIZE - circle_diameter) // 2
    draw.ellipse(
        (offset, offset, offset + circle_diameter, offset + circle_diameter),
        fill=BRAND_CIRCLE_COLOR,
    )
    canvas = Image.alpha_composite(canvas, circle_layer)

    # アイコンを、円の中でも十分な余白を残すサイズへ縮小して中央配置。
    icon_target_w = int(circle_diameter * ICON_RATIO_IN_CIRCLE)
    scale = icon_target_w / icon.width
    icon_target_h = int(icon.height * scale)
    icon_resized = icon.resize((icon_target_w, icon_target_h), Image.LANCZOS)

    paste_x = (MASTER_SIZE - icon_target_w) // 2
    paste_y = (MASTER_SIZE - icon_target_h) // 2
    canvas.alpha_composite(icon_resized, (paste_x, paste_y))

    return canvas


def main():
    master = build_favicon_master()
    print(f"faviconマスター画像: {master.size} (円背景 #{BRAND_CIRCLE_COLOR[0]:02X}{BRAND_CIRCLE_COLOR[1]:02X}{BRAND_CIRCLE_COLOR[2]:02X}, 四隅は透過)")

    sizes_png = {
        "favicon-16x16.png": 16,
        "favicon-32x32.png": 32,
        "favicon-48x48.png": 48,
        "favicon-96x96.png": 96,
        "apple-touch-icon.png": 180,
        "android-chrome-192x192.png": 192,
        "android-chrome-512x512.png": 512,
    }
    for filename, size in sizes_png.items():
        resized = master.resize((size, size), Image.LANCZOS)
        resized.save(os.path.join(ROOT, filename))
        print(f"  書き出し: {filename} ({size}x{size})")

    # favicon.ico: Googleの推奨(48px単位の正方形を含む)に合わせ、16/32/48の
    # マルチサイズICOにする。
    ico_16 = master.resize((16, 16), Image.LANCZOS)
    ico_32 = master.resize((32, 32), Image.LANCZOS)
    ico_48 = master.resize((48, 48), Image.LANCZOS)
    ico_48.save(
        os.path.join(ROOT, "favicon.ico"),
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
        append_images=[ico_16, ico_32, ico_48],
    )
    print("  書き出し: favicon.ico (16/32/48 マルチサイズ)")

    print("\n完了。")


if __name__ == "__main__":
    main()
