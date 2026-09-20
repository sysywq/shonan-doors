"""
generate_favicons.py
----------------------
新しい湘南Doors公式ロゴ(白背景版)から、favicon一式を生成する。
generate_ogp_images.py / generate_scenic_images.py と同じ位置づけの
「手動/一回限り実行するアセット生成スクリプト」であり、build.pyの
日次実行フローには含まれない。

小さいサイズ(16x16/32x32)では「Shonan Doors」の文字が潰れて読めなく
なるため、ロゴ下部の文字部分を含めず、アイコン(扉・太陽・海・波)部分
だけを正方形に切り出したソースを別途作り、そこから各サイズを書き出す。
大きいサイズ(apple-touch-icon 180x180・android-chrome 192/512)は、
文字を含めても十分視認できるため、ロゴ全体(白背景版)を使用する。

入力:
  /mnt/user-data/uploads/Large_Shonan_Doors_Logo_with_background.png
    (1254x1254, 白背景, アイコン+「Shonan Doors」文字を含むロゴ全体)

出力(リポジトリ直下):
  favicon.ico                  (16x16 + 32x32 のマルチサイズICO)
  favicon-16x16.png
  favicon-32x32.png
  apple-touch-icon.png         (180x180)
  android-chrome-192x192.png
  android-chrome-512x512.png
"""
import os
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE = "/mnt/user-data/uploads/Large_Shonan_Doors_Logo_with_background.png"

# ロゴ全体(1254x1254)のうち、上から何pxまでが「アイコン部分」かの目安。
# それ以降は「Shonan Doors」の文字部分になるため、favicon小サイズには含めない。
ICON_ONLY_CROP_HEIGHT = 1000


def build_icon_only_square(source_img):
    """文字を含まない、アイコン部分だけの正方形画像を作る。
    アイコン部分(横1254 x 縦1000)を、変形させずに1254x1254の白背景
    正方形へ上下均等に余白を追加して収める(引き伸ばし・トリミングなし)。"""
    w, h = source_img.size
    icon_crop = source_img.crop((0, 0, w, ICON_ONLY_CROP_HEIGHT))
    square = Image.new("RGB", (w, w), (255, 255, 255))
    pad_top = (w - ICON_ONLY_CROP_HEIGHT) // 2
    square.paste(icon_crop, (0, pad_top))
    return square


def main():
    full_logo = Image.open(SOURCE).convert("RGB")
    print(f"元ロゴ: {full_logo.size}")

    icon_only = build_icon_only_square(full_logo)
    print(f"アイコンのみ正方形(favicon小サイズ用): {icon_only.size}")

    # --- 小サイズ(文字を含まない、アイコンのみ) ---
    favicon_16 = icon_only.resize((16, 16), Image.LANCZOS)
    favicon_32 = icon_only.resize((32, 32), Image.LANCZOS)
    favicon_16.save(os.path.join(ROOT, "favicon-16x16.png"))
    favicon_32.save(os.path.join(ROOT, "favicon-32x32.png"))
    print("書き出し: favicon-16x16.png, favicon-32x32.png")

    # favicon.ico(16x16 + 32x32 のマルチサイズ)
    favicon_32.save(
        os.path.join(ROOT, "favicon.ico"),
        format="ICO",
        sizes=[(16, 16), (32, 32)],
    )
    print("書き出し: favicon.ico (16x16 + 32x32)")

    # --- 大サイズ(ロゴ全体。文字も十分視認できるため丸ごと使用) ---
    apple_touch = full_logo.resize((180, 180), Image.LANCZOS)
    android_192 = full_logo.resize((192, 192), Image.LANCZOS)
    android_512 = full_logo.resize((512, 512), Image.LANCZOS)
    apple_touch.save(os.path.join(ROOT, "apple-touch-icon.png"))
    android_192.save(os.path.join(ROOT, "android-chrome-192x192.png"))
    android_512.save(os.path.join(ROOT, "android-chrome-512x512.png"))
    print("書き出し: apple-touch-icon.png, android-chrome-192x192.png, android-chrome-512x512.png")

    print("\n完了。")


if __name__ == "__main__":
    main()
