"""
generate_favicons.py
----------------------
湘南Doors公式ロゴから、favicon専用に最適化した画像一式を生成する。
generate_ogp_images.py / generate_scenic_images.py と同じ位置づけの
「手動/一回限り実行するアセット生成スクリプト」であり、build.pyの
日次実行フローには含まれない。

【これまでの経緯】
v1: ロゴ画像の上部をそのまま正方形に収めただけ
    → 背景が正方形いっぱいの白塗りで、四隅にも背景色が残り、
      「四角い画像を縮小しただけ」に見える。
v2: 円形のブランドカラー背景+ロゴのフルカラーイラストを中央配置
    → 円形化はできたが、イラストの色(青い扉・金色の太陽等)が
      濃紺の背景に対してコントラストが弱く、かつ余白too大きめで
      アイコン自体が小さく、16px前後では視認性が低かった。
v3(今回): ロゴの色付きイラストをそのまま使うのではなく、
      「扉+アーチ」という最も象徴的な形だけを抽出した、
      白一色のシンプルなピクトグラムを新たに作成し、
      ブランドカラーの円形背景に大きく配置する。
    細い線・細かい装飾(太陽・鳥・波等)は小サイズで潰れるため
    favicon専用では省略し、シルエットではなく単純化した
    幾何学形状(角丸長方形の扉+半円アーチ)として再構成することで、
    - 白 vs 濃紺の最大コントラスト
    - 16pxでも判別できる太い形状
    - Instagram/ホットペッパー的な「小さくても存在感のあるシンボル」
    を実現する。あくまで「favicon用に最適化したバリエーション」であり、
    サイト本体のブランドロゴ画像(assets/images/brand/)自体は変更しない。

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

MASTER_SIZE = 1024

# 既存サイトのブランドカラー(assets/site.css の --trust と同じ値)。
BRAND_CIRCLE_COLOR = (0x1D, 0x35, 0x57, 255)  # #1D3557
ICON_COLOR = (255, 255, 255, 255)  # 白(最大コントラストのため)

# 円の直径 ÷ キャンバス全体の比率。四隅の透過マージンは最小限に抑えつつ、
# favicon表示時に円が窮屈に切れて見えない程度は残す。
CIRCLE_RATIO = 0.94
# ピクトグラムの幅 ÷ 円の直径の比率。円の中でアイコンが十分に大きく、
# かつ円からはみ出さない・詰まりすぎない範囲。
ICON_RATIO_IN_CIRCLE = 0.60


def draw_door_pictogram(size):
    """「扉+アーチ」のシンプルなピクトグラムを描画する(白一色、細い線なし)。
    元ロゴの色付きイラスト(太陽・鳥・波等の細部)は小サイズで潰れるため使わず、
    ブランドの核となる「開いた扉+アーチ型の入口」という形だけを、
    太く単純な2つの図形(角丸長方形+半円アーチ)で再構成する。"""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # 200x200の設計グリッドを実サイズへスケールする
    scale = size / 200

    def sc(*vals):
        return tuple(v * scale for v in vals)

    # 扉(左側の縦長パネル、角丸)
    door_box = sc(38, 55, 92, 172)
    door_radius = 8 * scale
    draw.rounded_rectangle(door_box, radius=door_radius, fill=ICON_COLOR)

    # アーチ(右側、上部が丸いドア枠) — 下部の矩形+上部の半円
    arch_rect = sc(92, 108, 168, 172)
    draw.rectangle(arch_rect, fill=ICON_COLOR)
    arch_dome = sc(92, 44, 168, 172)
    draw.pieslice(arch_dome, start=180, end=360, fill=ICON_COLOR)

    return canvas


def build_favicon_master():
    """ブランドカラーの円形背景+中央に白いピクトグラムを配置した、
    高解像度(MASTER_SIZE四方)のfavicon用マスター画像(RGBA・四隅透過)を作る。"""
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

    # ピクトグラムを、円の直径のICON_RATIO_IN_CIRCLE倍のサイズで描画し、
    # 実際の図形の非透過bounding boxを基準に中央配置する
    # (設計グリッド上でのマージンにアイコンの見かけサイズを左右されないため)。
    pictogram_master = draw_door_pictogram(MASTER_SIZE)
    bbox = pictogram_master.getbbox()
    pictogram_trimmed = pictogram_master.crop(bbox)

    icon_target_w = int(circle_diameter * ICON_RATIO_IN_CIRCLE)
    scale = icon_target_w / pictogram_trimmed.width
    icon_target_h = int(pictogram_trimmed.height * scale)
    icon_resized = pictogram_trimmed.resize((icon_target_w, icon_target_h), Image.LANCZOS)

    paste_x = (MASTER_SIZE - icon_target_w) // 2
    paste_y = (MASTER_SIZE - icon_target_h) // 2
    canvas.alpha_composite(icon_resized, (paste_x, paste_y))

    return canvas


def main():
    master = build_favicon_master()
    print(f"faviconマスター画像: {master.size} (円背景 #{BRAND_CIRCLE_COLOR[0]:02X}{BRAND_CIRCLE_COLOR[1]:02X}{BRAND_CIRCLE_COLOR[2]:02X}, "
          f"白ピクトグラム, 四隅は透過, circle_ratio={CIRCLE_RATIO}, icon_ratio={ICON_RATIO_IN_CIRCLE})")

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
