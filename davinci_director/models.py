"""The speech-recognition model (Parakeet TDT 0.6B v3 UK): downloaded once into
<program folder>\\models\\ihnatov-yuri--parakeet-tdt-0.6b-v3-uk, then reused.

Download: the parallel, resumable downloader, SHA-256 checked, pinned to one revision.
"""

import os

from . import downloader, parakeet
from .components import data_dir, folder_size

ASR = parakeet.NAME


class ModelError(Exception):
    pass


def models_dir():
    return os.path.join(data_dir(), "models")


def local_dir():
    return os.path.join(models_dir(), parakeet.REPO.replace("/", "--"))


def _complete(path):
    return parakeet.model_dir_ok(path) and os.path.isfile(os.path.join(path, ".complete"))


def available():
    return _complete(local_dir())


def _lfs_sha256(sibling):
    lfs = getattr(sibling, "lfs", None)
    return lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)


def ensure_asr(progress=None, cancel=None):
    """Path of the recognition model, downloading it (~2.5 GB) the first time."""
    dest = local_dir()
    if _complete(dest):
        return dest
    from huggingface_hub import HfApi, hf_hub_url
    try:
        info = HfApi().model_info(parakeet.REPO, files_metadata=True)
    except Exception as e:  # network / Hugging Face errors have many types; the message tells which
        raise ModelError("Не вдалося отримати модель розпізнавання %s (немає інтернету?): %s" % (parakeet.LABEL, e))
    items = [{"url": hf_hub_url(parakeet.REPO, s.rfilename, revision=info.sha), "dest": os.path.join(dest, s.rfilename),
              "size": s.size or None, "sha256": _lfs_sha256(s)}
             for s in info.siblings if s.rfilename in parakeet.FILES]
    missing = set(parakeet.FILES) - {os.path.basename(it["dest"]) for it in items}
    if missing:
        raise ModelError("У репозиторії %s немає файлів: %s" % (parakeet.REPO, ", ".join(sorted(missing))))
    try:
        downloader.download_many(items, progress=progress, cancel=cancel, label="Модель %s" % parakeet.LABEL)
    except downloader.DownloadError as e:
        raise ModelError("Модель %s: %s" % (parakeet.LABEL, e))
    with open(os.path.join(dest, ".complete"), "w") as f:
        f.write("%s@%s\n" % (parakeet.REPO, info.sha))
    return dest


def list_local():
    if not os.path.isdir(models_dir()):
        return []
    out = []
    for d in sorted(os.listdir(models_dir())):
        p = os.path.join(models_dir(), d)
        if os.path.isdir(p) and not d.endswith((".src", ".tmp")):
            out.append({"name": d.replace("--", "/"), "path": p, "size": downloader.human(folder_size(p)),
                        "complete": _complete(p)})
    return out
