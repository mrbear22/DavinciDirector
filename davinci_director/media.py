"""Media probing and audio extraction for transcription.

Uses PyAV (FFmpeg libraries bundled with faster-whisper), so no separate FFmpeg
install is needed; falls back to the ffmpeg/ffprobe executables if PyAV fails.
"""

import hashlib
import json
import os
import shutil
import subprocess
import wave

from .storage import cache_dir


class MediaError(Exception):
    pass


def _key(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
    return h.hexdigest()[:16]


# ------------------------------------------------------------------ PyAV


def _probe_av(path):
    import av
    with av.open(path) as c:
        duration = float(c.duration) / av.time_base if c.duration else 0.0
        info = {"duration": duration, "format": c.format.name if c.format else "", "video": None, "audio": None}
        for s in c.streams:
            if s.type == "video" and not info["video"]:
                rate = s.average_rate or s.guessed_rate or s.base_rate
                info["video"] = {"codec": s.codec_context.name, "width": s.codec_context.width,
                                 "height": s.codec_context.height, "fps": float(rate) if rate else 0.0}
                if not duration and s.duration and s.time_base:
                    info["duration"] = float(s.duration * s.time_base)
            elif s.type == "audio" and not info["audio"]:
                info["audio"] = {"codec": s.codec_context.name, "channels": s.codec_context.channels,
                                 "sample_rate": s.codec_context.sample_rate}
                if not info["duration"] and s.duration and s.time_base:
                    info["duration"] = float(s.duration * s.time_base)
    return info


def _extract_av(path, start, end, out, cancel=None, progress=None, sample_rate=16000):
    import av
    with av.open(path) as c:
        stream = next((s for s in c.streams if s.type == "audio"), None)
        if stream is None:
            raise MediaError("У файлі немає аудіо: %s" % path)
        stream.thread_type = "AUTO"
        if start > 0:
            c.seek(int(start * av.time_base), any_frame=False, backward=True)  # container time base
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        first = int(round(start * sample_rate))
        total = int(round((end - start) * sample_rate))
        written = 0
        span = max(end - start, 1e-6)
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)

            def put(frames):
                nonlocal written
                for f in frames:
                    if f.pts is None:
                        continue
                    pos = int(round(float(f.pts * f.time_base) * sample_rate))
                    data = f.to_ndarray().reshape(-1)
                    skip = max(0, first + written - pos)
                    if skip >= len(data):
                        continue
                    if pos > first + written and written == 0:
                        pass  # seeking landed after the start: the gap is silence anyway
                    data = data[skip:skip + (total - written)]
                    w.writeframes(data.tobytes())
                    written += len(data)

            for packet in c.demux(stream):
                if cancel and cancel():
                    raise MediaError("cancelled")
                for frame in packet.decode():
                    if frame.pts is not None and float(frame.pts * frame.time_base) < start - 1.0:
                        continue
                    put(resampler.resample(frame))
                    if written >= total:
                        break
                if written >= total:
                    break
                if progress:
                    progress(min(1.0, written / max(total, 1)))
            put(resampler.resample(None))
    if written == 0:
        raise MediaError("Не вдалося прочитати аудіо з %s" % path)
    return written / sample_rate


# ------------------------------------------------------------------ ffmpeg fallback


def ffmpeg_bin(name="ffmpeg"):
    env = os.environ.get("DD_" + name.upper())
    if env:
        return env
    found = shutil.which(name)
    if not found:
        raise MediaError("Не вдалося прочитати медіафайл (PyAV), а %s не знайдено." % name)
    return found


def _probe_ffmpeg(path):
    out = subprocess.run([ffmpeg_bin("ffprobe"), "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", path], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise MediaError("ffprobe: " + out.stderr.strip())
    data = json.loads(out.stdout)
    info = {"duration": float(data.get("format", {}).get("duration") or 0),
            "format": data.get("format", {}).get("format_name", ""), "video": None, "audio": None}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["video"]:
            num, _, den = (s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1").partition("/")
            fps = float(num) / float(den or 1) if float(den or 1) else 0
            info["video"] = {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height"), "fps": fps}
        if s.get("codec_type") == "audio" and not info["audio"]:
            info["audio"] = {"codec": s.get("codec_name"), "channels": s.get("channels"), "sample_rate": s.get("sample_rate")}
    return info


def _extract_ffmpeg(path, start, end, out, sample_rate=16000):
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-ss", "%.3f" % start, "-t", "%.3f" % (end - start),
           "-i", path, "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", out]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         creationflags=0x08000000 if os.name == "nt" else 0)
    if res.returncode != 0:
        raise MediaError("FFmpeg: " + (res.stderr.strip().splitlines() or ["failed"])[-1])


# ------------------------------------------------------------------ public


def probe(path):
    try:
        return _probe_av(path)
    except ImportError:
        return _probe_ffmpeg(path)
    except Exception as e:
        try:
            return _probe_ffmpeg(path)
        except MediaError:
            raise MediaError("Не вдалося прочитати %s: %s" % (path, e))


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
    try:
        _extract_av(path, start, end, tmp, cancel, progress, sample_rate)
    except MediaError as e:
        if str(e) == "cancelled":
            raise
        _extract_ffmpeg(path, start, end, tmp, sample_rate)
    except ImportError:
        _extract_ffmpeg(path, start, end, tmp, sample_rate)
    except Exception:
        _extract_ffmpeg(path, start, end, tmp, sample_rate)
    os.replace(tmp, out)
    return out
