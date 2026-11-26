"""FFmpeg / FFprobe helpers: probing, audio extraction, loudness, proxies."""

import hashlib
import json
import math
import os
import shutil
import subprocess
import wave

from .storage import cache_dir

BROWSER_VIDEO = {"h264", "vp8", "vp9", "av1"}
BROWSER_CONTAINERS = ("mp4", "mov", "webm", "matroska")


class MediaError(Exception):
    pass


def ffmpeg_bin(name="ffmpeg"):
    env = os.environ.get("DD_" + name.upper())
    if env:
        return env
    found = shutil.which(name)
    if not found:
        raise MediaError("%s не знайдено. Встановіть FFmpeg і додайте його в PATH." % name)
    return found


def _run(cmd, cancel=None, progress=None, duration=None):
    """Run ffmpeg with -progress parsing; raises MediaError on failure."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    try:
        for line in proc.stdout:
            if cancel and cancel():
                proc.kill()
                raise MediaError("cancelled")
            if progress and duration and line.startswith("out_time_us="):
                try:
                    progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
                except ValueError:
                    pass
        err = proc.stderr.read()
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        raise MediaError("FFmpeg error: " + err.strip().splitlines()[-1] if err.strip() else "FFmpeg failed")


def probe(path):
    out = subprocess.run([ffmpeg_bin("ffprobe"), "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", path], capture_output=True, text=True)
    if out.returncode != 0:
        raise MediaError("ffprobe: " + out.stderr.strip())
    data = json.loads(out.stdout)
    info = {"duration": float(data.get("format", {}).get("duration") or 0),
            "format": data.get("format", {}).get("format_name", ""), "video": None, "audio": None}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["video"]:
            rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
            num, _, den = rate.partition("/")
            fps = float(num) / float(den or 1) if float(den or 1) else 0
            info["video"] = {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height"),
                             "fps": fps, "pix_fmt": s.get("pix_fmt")}
        if s.get("codec_type") == "audio" and not info["audio"]:
            info["audio"] = {"codec": s.get("codec_name"), "channels": s.get("channels"),
                             "sample_rate": s.get("sample_rate")}
    return info


def browser_playable(info):
    v = info.get("video")
    if not v:
        return False
    fmt = info.get("format", "")
    return v["codec"] in BROWSER_VIDEO and any(c in fmt for c in BROWSER_CONTAINERS) and v.get("pix_fmt", "yuv420p") in (
        "yuv420p", "yuvj420p", None)


def _key(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
    return h.hexdigest()[:16]


def extract_audio(path, start, end, cancel=None, progress=None, sample_rate=16000):
    """Extract [start, end) seconds of ``path`` to 16 kHz mono WAV (cached)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        raise MediaError("Файл не знайдено: %s" % path)
    out = os.path.join(cache_dir("audio"), "%s.wav" % _key(path, mtime, round(start, 3), round(end, 3), sample_rate))
    if os.path.exists(out) and os.path.getsize(out) > 44:
        return out
    tmp = out + ".part.wav"
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-ss", "%.3f" % start, "-t", "%.3f" % (end - start),
           "-i", path, "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le",
           "-progress", "pipe:1", "-nostats", tmp]
    _run(cmd, cancel, progress, end - start)
    os.replace(tmp, out)
    return out


def loudness_profile(wav_path, hop=0.25):
    """Peak RMS loudness (dBFS) per ``hop`` seconds, streaming the WAV."""
    import array
    values = []
    with wave.open(wav_path, "rb") as w:
        sr = w.getframerate()
        n = max(1, int(sr * hop))
        width = w.getsampwidth()
        if width != 2:
            return hop, values
        while True:
            raw = w.readframes(n)
            if not raw:
                break
            buf = array.array("h")
            buf.frombytes(raw[: len(raw) - len(raw) % 2])
            if not buf:
                break
            try:
                import numpy as np
                a = np.frombuffer(buf, dtype=np.int16).astype("float32")
                rms = float(math.sqrt(float((a * a).mean()) + 1e-9))
            except ImportError:
                rms = math.sqrt(sum(x * x for x in buf) / len(buf) + 1e-9)
            values.append(round(20 * math.log10(rms / 32768.0 + 1e-12), 1))
    return hop, values


def make_proxy(path, cancel=None, progress=None, height=540):
    """Browser-friendly H.264 preview proxy for the in-panel viewer."""
    mtime = os.path.getmtime(path)
    out = os.path.join(cache_dir("proxy"), "%s.mp4" % _key(path, mtime, height))
    if os.path.exists(out) and os.path.getsize(out) > 1000:
        return out
    duration = probe(path)["duration"]
    tmp = out + ".part.mp4"
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", "scale=-2:%d" % height, "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
           "-pix_fmt", "yuv420p", "-g", "50", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
           "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", tmp]
    _run(cmd, cancel, progress, duration)
    os.replace(tmp, out)
    return out


def existing_proxy(path, height=540):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    out = os.path.join(cache_dir("proxy"), "%s.mp4" % _key(path, mtime, height))
    return out if os.path.exists(out) else None
