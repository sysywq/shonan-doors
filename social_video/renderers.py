# -*- coding: utf-8 -*-
"""renderer/provider interface と実装。

renderer は manifest(JSON)だけを入力にして 9:16 の MP4(H.264 + AAC)を作る。
動画テイストが決まったら、ここに新しい renderer を追加して config.json の renderer.name を
切り替えるだけでよい(manifest・storage・publisher 側は変更不要)。

同梱の実装:
  ffmpeg_slideshow … 記事に掲載済みの画像をシーンごとに静止表示する仮の renderer。
                     字幕は renderer.options.font_file(日本語フォント)があるときだけ焼き込む。
                     BGM は renderer.options.audio_file があれば使い、無ければ無音のAACトラックを入れる
                     (H.264 + AAC を常に満たすため)。各SNSのwatermarkは一切焼き込まない。
  command          … 任意の外部コマンド(動画生成AIのCLI等)に manifest を渡して MP4 を作らせる。
                     renderer.command に "{manifest}" "{output}" "{work_dir}" を含めて指定する。

どの renderer でも、最後に validate_output() で共通の安全仕様を確認する。
"""
import json
import os
import shlex
import subprocess
import tempfile

from . import articles as art

# 日本語字幕用フォントの既定探索先(ubuntu-latest で fonts-noto-cjk を入れた場合など)
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


class RenderError(Exception):
    pass


class Renderer:
    """renderer の共通 interface。"""

    name = "base"

    def __init__(self, cfg, *, run=subprocess.run, http=None):
        self.cfg = cfg
        self.run = run
        self.http = http

    def render(self, manifest, out_path, work_dir):
        """manifest から out_path に MP4 を書き出す。戻り値は validate_output() の結果。"""
        raise NotImplementedError


def _ffmpeg_quote(path):
    """filtergraph 内のオプション値として安全に渡すためのエスケープ。"""
    return "'" + str(path).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:") + "'"


def _double_rate(rate):
    """"8M" → "16M"(VBVバッファはmaxrateの2倍)。"""
    rate = str(rate)
    unit = rate[-1] if rate[-1:].isalpha() else ""
    num = float(rate[:-1] if unit else rate)
    return f"{num * 2:g}{unit}"


def wrap_text(text, width=14):
    """日本語は単語区切りが無いため、文字数で折り返す(字幕デザインは未確定の仮実装)。"""
    lines = []
    for raw in (text or "").split("\n"):
        while len(raw) > width:
            lines.append(raw[:width])
            raw = raw[width:]
        lines.append(raw)
    return "\n".join(lines)


class FfmpegSlideshowRenderer(Renderer):
    name = "ffmpeg_slideshow"

    def _resolve_image(self, url, work_dir, i):
        local = art.local_path_for_url(url)
        if local:
            return local
        if self.http is None:
            raise RenderError(f"画像を取得できません(ローカルに無く、HTTPクライアント未設定): {url}")
        resp = self.http.request("GET", url, timeout=60)
        ext = os.path.splitext(url.split("?")[0])[1] or ".img"
        path = os.path.join(work_dir, f"image_{i}{ext}")
        with open(path, "wb") as f:
            f.write(resp.body)
        return path

    def _font(self):
        opt = self.cfg["renderer"].get("options") or {}
        if opt.get("font_file"):
            if not os.path.isfile(opt["font_file"]):
                raise RenderError(f"renderer.options.font_file が見つかりません: {opt['font_file']}")
            return opt["font_file"]
        for c in FONT_CANDIDATES:
            if os.path.isfile(c):
                return c
        return ""

    def build_command(self, manifest, out_path, work_dir):
        out = self.cfg["output"]
        opt = self.cfg["renderer"].get("options") or {}
        w, h, fps = out["width"], out["height"], out["fps"]
        scenes = manifest["scenes"]
        if not scenes:
            raise RenderError("manifest にシーンがありません")
        font = self._font()
        if not font:
            print("  [renderer] 日本語フォントが無いため字幕を焼き込みません(renderer.options.font_file で指定可)")

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        cache = {}
        for s in scenes:
            url = s.get("image_url") or (manifest.get("source_image_urls") or [""])[0]
            if not url:
                raise RenderError(f"シーン{s['index']}に画像がありません")
            if url not in cache:
                cache[url] = self._resolve_image(url, work_dir, len(cache))
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", str(s["duration_sec"]), "-i", cache[url]]
        n = len(scenes)
        if opt.get("audio_file"):
            cmd += ["-stream_loop", "-1", "-i", opt["audio_file"]]
        else:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={out['audio_sample_rate']}"]

        chains = []
        for i, s in enumerate(scenes):
            chain = (f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                     f"setsar=1,fps={fps},format={out['pix_fmt']}")
            if font and s.get("text"):
                tf = os.path.join(work_dir, f"scene_{i}.txt")
                with open(tf, "w", encoding="utf-8") as f:
                    f.write(wrap_text(s["text"]))
                box = ":box=1:boxcolor=black@0.45:boxborderw=24" if opt.get("text_box", True) else ""
                chain += (f",drawtext=fontfile={_ffmpeg_quote(font)}:textfile={_ffmpeg_quote(tf)}:expansion=none"
                          f":fontcolor={opt.get('font_color', 'white')}:fontsize={int(w * 0.055)}"
                          f":line_spacing=12{box}:x=(w-text_w)/2:y=h*0.68")
            chains.append(chain + f"[v{i}]")
        chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]")
        total = round(sum(float(s["duration_sec"]) for s in scenes), 2)
        gop = int(fps) * 2
        cmd += [
            "-filter_complex", ";".join(chains),
            "-map", "[vout]", "-map", f"{n}:a",
            "-c:v", out["video_codec"], "-profile:v", out["video_profile"], "-level:v", str(out["video_level"]),
            "-pix_fmt", out["pix_fmt"], "-preset", "medium", "-crf", str(out["crf"]),
            "-maxrate", out["max_video_bitrate"], "-bufsize", _double_rate(out["max_video_bitrate"]),
            "-r", str(fps), "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-c:a", out["audio_codec"], "-b:a", out["audio_bitrate"], "-ar", str(out["audio_sample_rate"]), "-ac", "2",
            "-t", str(total), "-movflags", "+faststart", out_path,
        ]
        return cmd

    def render(self, manifest, out_path, work_dir):
        cmd = self.build_command(manifest, out_path, work_dir)
        r = self.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RenderError(f"ffmpeg が失敗しました: {(r.stderr or '')[-800:]}")
        return validate_output(out_path, self.cfg, run=self.run)


class CommandRenderer(Renderer):
    """外部コマンドに manifest を渡す renderer(動画生成AI等の差し込み口)。"""

    name = "command"

    def render(self, manifest, out_path, work_dir):
        template = (self.cfg["renderer"].get("command") or "").strip()
        if not template:
            raise RenderError("renderer.name=command には renderer.command の指定が必要です")
        mpath = os.path.join(work_dir, "manifest.json")
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        cmd = [part.format(manifest=mpath, output=out_path, work_dir=work_dir) for part in shlex.split(template)]
        r = self.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RenderError(f"renderer.command が失敗しました(exit {r.returncode}): {(r.stderr or '')[-800:]}")
        return validate_output(out_path, self.cfg, run=self.run)


RENDERERS = {
    FfmpegSlideshowRenderer.name: FfmpegSlideshowRenderer,
    CommandRenderer.name: CommandRenderer,
}


def get_renderer(cfg, **deps):
    name = cfg["renderer"]["name"]
    if name not in RENDERERS:
        raise RenderError(f"未知の renderer です: {name}(利用可能: {', '.join(sorted(RENDERERS))})")
    return RENDERERS[name](cfg, **deps)


def validate_output(path, cfg, *, run=subprocess.run):
    """全SNS共通で使える安全仕様かを ffprobe で確認する。
    H.264 / yuv420p / 9:16 / AAC / 尺が min〜max 秒。問題があれば RenderError。"""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise RenderError(f"出力ファイルがありません: {path}")
    r = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
            capture_output=True, text=True)
    if r.returncode != 0:
        raise RenderError(f"ffprobe が失敗しました: {(r.stderr or '')[-400:]}")
    info = json.loads(r.stdout or "{}")
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out = cfg["output"]
    problems = []
    if not v or v.get("codec_name") != "h264":
        problems.append(f"映像コーデックがH.264ではありません({v and v.get('codec_name')})")
    if v and v.get("pix_fmt") != "yuv420p":
        problems.append(f"pix_fmt が yuv420p ではありません({v.get('pix_fmt')})")
    if v and int(v.get("width", 0)) * 16 != int(v.get("height", 0)) * 9:
        problems.append(f"9:16 ではありません({v.get('width')}x{v.get('height')})")
    if not a or a.get("codec_name") != "aac":
        problems.append(f"音声コーデックがAACではありません({a and a.get('codec_name')})")
    duration = float((info.get("format") or {}).get("duration") or 0)
    if not (out["min_duration_sec"] <= duration <= out["max_duration_sec"] + 0.5):
        problems.append(f"尺が {out['min_duration_sec']}〜{out['max_duration_sec']}秒の範囲外です({duration:.2f}秒)")
    if problems:
        raise RenderError("出力が共通仕様を満たしません: " + " / ".join(problems))
    return {
        "width": int(v["width"]), "height": int(v["height"]), "duration_sec": round(duration, 2),
        "video_codec": "h264", "audio_codec": "aac", "size_bytes": os.path.getsize(path),
    }


def new_work_dir():
    return tempfile.mkdtemp(prefix="social_video_")
