"""
generate_favicons.py
----------------------
湘南Doorsの最終favicon/app iconデザイン画像
(Large_Shonan_Doors_Logo_with_background.png = 開いた扉の向こうに
 朝日・海・山が広がる、湘南Doorsロゴの中核シーン)から、
favicon一式を生成する。
generate_ogp_images.py / generate_scenic_images.py と同じ位置づけの
「手動/一回限り実行するアセット生成スクリプト」であり、build.pyの
日次実行フローには含まれない。

【デザイン方針】
ロゴ全体(扉+アーチ+太陽+山+海+鳥+下部の波飾り+「Shonan Doors」文字)の
うち、favicon向けには次の理由で中核シーンだけを正方形に切り出して使う。

- 鳥(画面右側、アーチの外)は小サイズでノイズになるため除外する
  (要件で明示的に許容されている簡略化)。
- 下部の波+水しぶきの装飾線と「Shonan Doors」の文字は、極小サイズでは
  判読できずノイズになるため除外する(海岸線に沿う波のカーブ自体は
  「湘南らしい波」としてクロップ範囲内にわずかに残しており、
  「湘南らしさ」の表現は维持している)。
- 残す要素: 開いた扉・アーチ・朝日・山のシルエット・海・波打ち際
  (要件にある「ドア/太陽/海/波/山・海岸線」をすべて満たす)。

正方形に切り出した後、円形にマスクしてキャンバスへ配置する
(円の外側=四隅は透過。円自体はキャンバスに対してできるだけ大きく、
外周の余白を最小限にする)。

入力:
  /mnt/user-data/uploads/Large_Shonan_Doors_Logo_with_background.png
    (1254x1254, 白背景, 湘南Doorsロゴ全体)

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
SOURCE = "/mnt/user-data/uploads/Large_Shonan_Doors_Logo_with_background.png"

# 元画像(1254x1254)から、扉+アーチ+太陽+山+海の中核シーンだけを
# 正方形に切り出す範囲(鳥・下部の波飾り・文字は除外)。
CORE_SCENE_BOX = (150, 90, 970, 910)  # -> 820x820の正方形

MASTER_SIZE = 1024
# 円の直径 ÷ キャンバス全体の比率。外周の透過マージンは最小限にする。
CIRCLE_RATIO = 0.96


def build_favicon_master():
    """中核シーンを円形にマスクした、高解像度(MASTER_SIZE四方)の
    favicon用マスター画像(RGBA・四隅透過)を作る。"""
    src = Image.open(SOURCE).convert("RGB")
    core = src.crop(CORE_SCENE_BOX)
    core = core.resize((MASTER_SIZE, MASTER_SIZE), Image.LANCZOS).convert("RGBA")

    # 円形マスク(アンチエイリアスのため4倍解像度で作ってから縮小する)
    mask_super = Image.new("L", (MASTER_SIZE * 4, MASTER_SIZE * 4), 0)
    mdraw = ImageDraw.Draw(mask_super)
    circle_diameter_super = int(MASTER_SIZE * 4 * CIRCLE_RATIO)
    off_super = (MASTER_SIZE * 4 - circle_diameter_super) // 2
    mdraw.ellipse(
        (off_super, off_super, off_super + circle_diameter_super, off_super + circle_diameter_super),
        fill=255,
    )
    mask = mask_super.resize((MASTER_SIZE, MASTER_SIZE), Image.LANCZOS)

    canvas = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    canvas.paste(core, (0, 0), mask)
    return canvas


def main():
    master = build_favicon_master()
    print(f"faviconマスター画像: {master.size} (中核シーンを円形マスク, circle_ratio={CIRCLE_RATIO}, 四隅は透過)")

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
