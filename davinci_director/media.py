"""Audio of a media file for transcription: 16 kHz mono WAV pieces, cached.

Decoding uses PyAV (FFmpeg libraries), which arrives with the downloaded "audio" component.
"""

import hashlib
import os
import wave

from . import Cancelled
from .storage import cache_dir

SAMPLE_RATE = 16000
SEEK_SLACK = 1.0  # s: frames this far before the start are skipped without resampling


class MediaError(Exception):
    pass


def duration(path):
    import av
    try:
        with av.open(path) as c:
            if c.duration:
                return float(c.duration) / av.time_base
            s = next((s for s in c.streams if s.duration and s.time_base), None)
            return float(s.duration * s.time_base) if s else 0.0
    except av.FFmpegError as e:
        raise MediaError("Не вдалося прочитати %s: %s" % (path, e))


def extract_audio(path, start, end, cancel=None, progress=None):
    """[start, end) seconds of ``path`` -> path of a 16 kHz mono WAV (cached by file, time and range)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        raise MediaError("Файл не знайдено: %s" % path)
    key = hashlib.sha1(("%s|%s|%.3f|%.3f" % (path, mtime, start, end)).encode("utf-8")).hexdigest()[:16]
    out = os.path.join(cache_dir("audio"), key + ".wav")
    if os.path.exists(out) and os.path.getsize(out) > 44:
        return out
    tmp = out + ".part.wav"
    import av
    try:
        with av.open(path) as c, wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            written = _decode(c, path, start, end, w, cancel, progress)
    except av.FFmpegError as e:
        raise MediaError("Не вдалося прочитати аудіо з %s: %s" % (path, e))
    if not written:
        raise MediaError("Не вдалося прочитати аудіо з %s" % path)
    os.replace(tmp, out)
    return out


def _decode(container, path, start, end, wav, cancel, progress):
    """Writes the samples of [start, end) into ``wav``; returns how many were written."""
    import av
    stream = next((s for s in container.streams if s.type == "audio"), None)
    if stream is None:
        raise MediaError("У файлі немає аудіо: %s" % path)
    stream.thread_type = "AUTO"
    if start > 0:
        container.seek(int(start * av.time_base), any_frame=False, backward=True)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    first, total = int(round(start * SAMPLE_RATE)), int(round((end - start) * SAMPLE_RATE))
    written = 0

    def put(frames):
        nonlocal written
        for f in frames:
            if f.pts is None or written >= total:
                continue
            pos = int(round(float(f.pts * f.time_base) * SAMPLE_RATE))
            data = f.to_ndarray().reshape(-1)
            skip = max(0, first + written - pos)
            data = data[skip:skip + total - written]
            wav.writeframes(data.tobytes())
            written += len(data)

    for packet in container.demux(stream):
        if cancel and cancel():
            raise Cancelled()
        for frame in packet.decode():
            if frame.pts is None or float(frame.pts * frame.time_base) >= start - SEEK_SLACK:
                put(resampler.resample(frame))
        if written >= total:
            break
        if progress:
            progress(min(1.0, written / max(total, 1)))
    put(resampler.resample(None))
    return written
