"""Parallel ranged downloads: resume, retries after dropped connections, SHA-256."""

import hashlib
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from davinci_director import downloader

DATA = os.urandom(5 * 1024 * 1024 + 123)  # > 2 segments of 1 MB (set below)
SMALL = b"hello wheel" * 1000


class Handler(BaseHTTPRequestHandler):
    drops = {"n": 0}
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = DATA if self.path.startswith("/big") else SMALL
        rng = self.headers.get("Range")
        Handler.requests.append(rng)
        if rng:
            a, _, b = rng.split("=")[1].partition("-")
            a, b = int(a), int(b) if b else len(body) - 1
            chunk = body[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, len(body)))
        else:
            chunk = body
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        if self.path.startswith("/big") and Handler.drops["n"] > 0 and len(chunk) > 100000:
            Handler.drops["n"] -= 1
            self.wfile.write(chunk[:50000])  # connection drops in the middle
            return
        self.wfile.write(chunk)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(downloader, "SEGMENT", 1024 * 1024)
    monkeypatch.setattr(downloader.time, "sleep", lambda s: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def test_parallel_segments_retry_and_verify(server, tmp_path):
    Handler.drops["n"] = 3
    Handler.requests.clear()
    seen = []
    items = [{"url": server + "/big", "dest": str(tmp_path / "model.bin"), "sha256": hashlib.sha256(DATA).hexdigest()},
             {"url": server + "/small", "dest": str(tmp_path / "a.whl")}]
    downloader.download_many(items, progress=lambda f, m: seen.append((f, m)), connections=4)
    assert open(tmp_path / "model.bin", "rb").read() == DATA
    assert open(tmp_path / "a.whl", "rb").read() == SMALL
    assert sum(1 for r in Handler.requests if r and r != "bytes=0-0") >= 6, "big file fetched in ranges"
    assert seen[-1][0] == 1.0 and "МБ" in seen[-2][1]
    assert not any(f.endswith(".part") or f.endswith(".json") for f in os.listdir(tmp_path))


def test_bad_checksum_is_rejected(server, tmp_path):
    Handler.drops["n"] = 0
    with pytest.raises(downloader.DownloadError):
        downloader.download_many([{"url": server + "/big", "dest": str(tmp_path / "x.bin"), "sha256": "0" * 64}])
    assert not os.path.exists(tmp_path / "x.bin")


def test_resume_after_cancel(server, tmp_path):
    Handler.drops["n"] = 0
    dest = str(tmp_path / "m.bin")
    calls = {"n": 0}

    def cancel_soon():
        calls["n"] += 1
        return calls["n"] > 12

    with pytest.raises(downloader.Cancelled):
        downloader.download_many([{"url": server + "/big", "dest": dest}], cancel=cancel_soon, connections=2)
    assert os.path.exists(dest + ".part") and not os.path.exists(dest)
    Handler.requests.clear()
    downloader.download_many([{"url": server + "/big", "dest": dest}], connections=2)
    assert open(dest, "rb").read() == DATA
    full = [r for r in Handler.requests if r and r != "bytes=0-0"]
    assert len(full) < 6, "finished segments were not downloaded again"


def test_asr_model_download(server, tmp_path, monkeypatch):
    """Model files come from the Hugging Face listing (sizes + SHA-256, pinned revision) via the parallel
    downloader, once; README and the Core ML copies in the repo are not downloaded."""
    import types
    from davinci_director import models, parakeet
    monkeypatch.setenv("DD_ROOT", str(tmp_path))
    Handler.drops["n"] = 1
    lfs = types.SimpleNamespace(sha256=hashlib.sha256(DATA).hexdigest())
    siblings = [types.SimpleNamespace(rfilename="model.safetensors", size=len(DATA), lfs=lfs)]
    siblings += [types.SimpleNamespace(rfilename=f, size=len(SMALL), lfs=None)
                 for f in parakeet.FILES if f != "model.safetensors"]
    siblings += [types.SimpleNamespace(rfilename=f, size=10, lfs=None)
                 for f in ("README.md", "coreml/int8/parakeet-tdt-0.6b-v3/Encoder.mlmodelc/weights/weight.bin")]
    info = types.SimpleNamespace(sha="abc", siblings=siblings)
    urls = []

    class Api:
        def model_info(self, repo, files_metadata=False):
            assert repo == parakeet.REPO
            return info

    def url(repo, f, revision=None):
        urls.append((f, revision))
        return server + ("/big" if f == "model.safetensors" else "/small")

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "hf_hub_url", url)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    path = models.ensure_asr()
    assert open(os.path.join(path, "model.safetensors"), "rb").read() == DATA
    assert sorted(f for f, _ in urls) == sorted(parakeet.FILES) and all(r == "abc" for _, r in urls)
    assert not os.path.exists(os.path.join(path, "README.md")) and not os.path.exists(os.path.join(path, "coreml"))
    assert models.cached() == path
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: pytest.fail("second call must not touch the network"))
    assert models.ensure_asr() == path


def test_asr_model_reused_from_hf_cache(tmp_path, monkeypatch):
    from davinci_director import models, parakeet
    monkeypatch.setenv("DD_ROOT", str(tmp_path))
    snap = tmp_path / "hf"
    snap.mkdir()
    for f in parakeet.FILES:
        (snap / f).write_bytes(b"x")
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: str(snap))
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: pytest.fail("no download"))
    assert models.ensure_asr() == str(snap)
