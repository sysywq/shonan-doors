# -*- coding: utf-8 -*-
"""設定の読み込み。既定値は social_video/config.json、上書きは --config で渡す別JSON。

上書きJSONは既定値に再帰的にマージする(指定したキーだけ変わる)。
"""
import copy
import json
import os

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PKG_DIR, "config.json")

PLATFORMS = ("instagram", "facebook", "youtube", "tiktok")


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path=None, overrides=None):
    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if path:
        with open(path, encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))
    if overrides:
        cfg = deep_merge(cfg, overrides)
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    out = cfg["output"]
    if out["width"] * 16 != out["height"] * 9:
        raise ValueError(f"出力解像度は9:16にしてください({out['width']}x{out['height']})")
    if out["width"] % 2 or out["height"] % 2:
        raise ValueError("H.264(yuv420p)では幅・高さを偶数にしてください")
    d = cfg["manifest"]["duration_target_sec"]
    if not (out["min_duration_sec"] <= d <= out["max_duration_sec"]):
        raise ValueError(f"duration_target_sec={d} は {out['min_duration_sec']}〜{out['max_duration_sec']}秒の範囲にしてください")
    for name, p in cfg["platforms"].items():
        if name not in PLATFORMS:
            raise ValueError(f"未知のplatformです: {name}")
        if p.get("mode", "auto") not in ("auto", "manual"):
            raise ValueError(f"platforms.{name}.mode は auto / manual のどちらかです")
    tk = cfg["platforms"]["tiktok"]
    if tk.get("post_mode") not in ("direct", "upload"):
        raise ValueError("platforms.tiktok.post_mode は direct(Direct Post) / upload(下書き・inbox) のどちらかです")
    if tk.get("source") not in ("FILE_UPLOAD", "PULL_FROM_URL"):
        raise ValueError("platforms.tiktok.source は FILE_UPLOAD / PULL_FROM_URL のどちらかです")
