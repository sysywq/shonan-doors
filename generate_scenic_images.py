"""
generate_scenic_images.py
--------------------------
build.py内で定義している「写真風」の風景イラスト(CATEGORY_HERO_VISUALS /
TOP_HERO_VISUAL / AREA_HERO_VISUALS)を、実際のSVGファイルとして
assets/images/ 配下に書き出す。

これはgenerate_ogp_images.py と同じ位置づけの「手動/一回限り実行する
アセット生成スクリプト」であり、build.py の日次実行フローには含まれない。
実写真・AI生成画像を用意した場合は、同じファイル名で差し替えるだけでよい
(assets/images/categories/tourism.jpg のように拡張子を変えて配置し、
 build.py側のCATEGORY_IMAGE_FALLBACKの参照を更新する運用を想定)。

出力先:
  assets/images/hero/top.svg
  assets/images/categories/{tourism,gourmet,business,people,culture,event,life}.svg
  assets/images/areas/{fujisawa,chigasaki,kamakura,hiratsuka,oiso,ninomiya,zushi,hayama}.svg
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build as b

ROOT = os.path.dirname(os.path.abspath(__file__))
IMAGES_ROOT = os.path.join(ROOT, "assets", "images")


def write_svg(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  書き出し: {os.path.relpath(path, ROOT)} ({len(content)} bytes)")


def main():
    print("=== Hero(トップページ) ===")
    write_svg(os.path.join(IMAGES_ROOT, "hero", "top.svg"), b.TOP_HERO_VISUAL)

    print("=== カテゴリー別ビジュアル ===")
    for cat_key, svg in b.CATEGORY_HERO_VISUALS.items():
        en = b.CAT_EN[cat_key]
        write_svg(os.path.join(IMAGES_ROOT, "categories", f"{en}.svg"), svg)

    print("=== エリア別ビジュアル ===")
    for area_ja, svg in b.AREA_HERO_VISUALS.items():
        en = b.AREA_EN[area_ja]
        write_svg(os.path.join(IMAGES_ROOT, "areas", f"{en}.svg"), svg)

    print("\n完了。")


if __name__ == "__main__":
    main()
