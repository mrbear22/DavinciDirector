"""The speech-recognition model (Parakeet TDT 0.6B v3 UK): downloaded once, then reused.

Where it is looked for, in order:
  1. <program folder>\\models\\ihnatov-yuri--parakeet-tdt-0.6b-v3-uk (our own copy, marked complete);
  2. the Hugging Face cache (HF_HOME = <program folder>\\hf, see storage.setup_env), if it is there;
otherwise it is downloaded into (1) with the parallel, resumable downloader (SHA-256 checked, pinned revision).
"""

import fnmatch
import os

from . import downloader, parakeet
from .components import data_dir

ASR = parakeet.NAME


class ModelError(Exception):
    pass


def models_dir():
    return os.path.join(data_dir(), "models")


def local_dir(repo=parakeet.REPO):
    return os.path.join(models_dir(), repo.replace("/", "--"))


def _complete(path):
    return parakeet.model_dir_ok(path) and os.path.isfile(os.path.join(path, ".complete"))


def cached(name=ASR):
    """Path of the already available model or None."""
    if name != ASR:
        return None
    if _complete(local_dir()):
        return local_dir()
    try:
        from huggingface_hub import snapshot_download
        path = snapshot_download(parakeet.REPO, local_files_only=True, allow_patterns=parakeet.FILES)
        if parakeet.model_dir_ok(path):
            return path
    except Exception:
        pass
    return None


def ensure_asr(progress=None, cancel=None):
    """Path of the recognition model, downloading it (~2.5 GB) the first time."""
    path = cached()
    if path:
        return path
    try:
        from huggingface_hub import HfApi, hf_hub_url
        info = HfApi().model_info(parakeet.REPO, files_metadata=True)
    except Exception as e:
        raise ModelError("Не вдалося отримати модель розпізнавання %s (немає інтернету?): %s" % (parakeet.LABEL, e))
    dest = local_dir()
    items = []
    for s in info.siblings:
        if any(fnmatch.fnmatch(s.rfilename, p) for p in parakeet.FILES):
            lfs = getattr(s, "lfs", None)
            sha = getattr(lfs, "sha256", None) if lfs is not None else None
            if isinstance(lfs, dict):
                sha = lfs.get("sha256")
            items.append({"url": hf_hub_url(parakeet.REPO, s.rfilename, revision=info.sha),
                          "dest": os.path.join(dest, s.rfilename), "size": s.size or None, "sha256": sha})
    missing = set(parakeet.FILES) - {os.path.basename(it["dest"]) for it in items}
    if missing:
        raise ModelError("У репозиторії %s немає файлів: %s" % (parakeet.REPO, ", ".join(sorted(missing))))
    try:
        downloader.download_many(items, progress=progress, cancel=cancel, label="Модель %s" % parakeet.LABEL)
    except downloader.Cancelled:
        raise
    except downloader.DownloadError as e:
        raise ModelError("Модель %s: %s" % (parakeet.LABEL, e))
    with open(os.path.join(dest, ".complete"), "w") as f:
        f.write("%s@%s\n" % (parakeet.REPO, info.sha))
    return dest


def list_local():
    out = []
    if os.path.isdir(models_dir()):
        for d in sorted(os.listdir(models_dir())):
            p = os.path.join(models_dir(), d)
            if os.path.isdir(p) and not d.endswith((".src", ".tmp")):
                from .components import folder_size
                out.append({"name": d.replace("--", "/"), "path": p, "size": downloader.human(folder_size(p)),
                            "complete": _complete(p)})
    return out
