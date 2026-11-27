"""Component checks: no parallel first imports, and never a reinstall over a copy that is already loaded."""

import importlib
import os
import sys
import threading
import time

import pytest

from davinci_director import components


@pytest.fixture
def fake_component(tmp_path, monkeypatch):
    monkeypatch.setenv("DD_ROOT", str(tmp_path))
    monkeypatch.setattr(components, "_ok", set())
    monkeypatch.setattr(components, "errors", {})
    monkeypatch.setitem(components.COMPONENTS, "fake", {"label": "тестовий компонент", "size": "1 МБ",
                                                        "check": ["ddfake_pkg"], "pip": ["ddfake_pkg"]})
    monkeypatch.setattr(components, "ORDER", components.ORDER + ("fake",))
    yield components.component_dir("fake")
    for m in [m for m in sys.modules if m.startswith("ddfake_")]:
        del sys.modules[m]
    sys.path[:] = [p for p in sys.path if str(tmp_path) not in p]


def test_checks_run_one_at_a_time_and_are_cached(fake_component, monkeypatch):
    active, peak, calls = [0], [0], []

    def slow_import(mod):
        calls.append(mod)
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.2)  # like the first import of torch
        active[0] -= 1

    monkeypatch.setattr(components.importlib, "import_module", slow_import)
    results = []
    threads = [threading.Thread(target=lambda: results.append(components.installed("fake"))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True, True, True]
    assert peak[0] == 1, "checks overlapped"
    assert calls == ["ddfake_pkg"], "a successful check is remembered"


def test_failed_import_keeps_the_reason(fake_component):
    assert components.installed("fake") is False
    assert "ModuleNotFoundError" in components.errors["fake"] and "ddfake_pkg" in components.errors["fake"]


def test_no_reinstall_over_a_loaded_component(fake_component, monkeypatch):
    """The component's folder is in use by this process but its check fails (e.g. a broken import):
    replacing the folder would delete running libraries, so the user is asked to restart instead."""
    os.makedirs(fake_component)
    with open(os.path.join(fake_component, "ddfake_loaded.py"), "w") as f:
        f.write("X = 1\n")
    with open(os.path.join(fake_component, "ddfake_pkg.py"), "w") as f:
        f.write("raise ImportError('half-initialized')\n")
    components.activate()
    importlib.import_module("ddfake_loaded")  # code from the folder is running
    monkeypatch.setattr(components, "_plan", lambda *a: pytest.fail("must not download a reinstall"))
    with pytest.raises(components.ComponentError) as e:
        components.ensure("fake")
    assert "перезапустіть" in str(e.value) and "half-initialized" in str(e.value)
    assert os.path.isfile(os.path.join(fake_component, "ddfake_loaded.py")), "files left intact"


def test_missing_component_still_installs(fake_component, monkeypatch):
    """Nothing loaded from the folder (it does not exist yet): the normal download + install path runs."""
    planned = []
    monkeypatch.setattr(components, "_plan", lambda spec, report, cancel, extra=(): planned.append(spec) or [])
    monkeypatch.setattr(components.downloader, "download_many", lambda *a, **k: None)

    def fake_pip(args, cancel=None, env=None):
        tmp = args[args.index("--target") + 1]
        os.makedirs(tmp, exist_ok=True)
        with open(os.path.join(tmp, "ddfake_pkg.py"), "w") as f:
            f.write("OK = True\n")
        return 0, []

    monkeypatch.setattr(components, "_pip", fake_pip)
    assert components.ensure("fake") is True
    assert planned == [["ddfake_pkg"]] and components.installed("fake")


def test_install_reports_progress_by_bytes_written(tmp_path, monkeypatch):
    """Unpacking torch takes minutes: the bar moves with the bytes pip writes (its temp folder + target)."""
    import zipfile
    monkeypatch.setenv("DD_ROOT", str(tmp_path))
    wheel = tmp_path / "pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("pkg/big.bin", b"\0" * 400_000)  # unpacked size 400 kB, pip writes it twice
    seen = []

    def fake_pip(args, cancel=None, env=None):
        tmp = args[args.index("--target") + 1]
        for step in range(4):  # 4 x 200 kB = 2 x 400 kB
            folder = env["TEMP"] if step < 2 else tmp
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "part%d" % step), "wb") as f:
                f.write(b"\0" * 200_000)
            time.sleep(1.7)
        return 0, []

    monkeypatch.setattr(components, "_pip", fake_pip)
    code, _ = components._install(["pkg"], str(tmp_path), [str(wheel)], str(tmp_path / "out.new"), "Мовці",
                                  lambda x, m: seen.append((x, m)), None)
    assert code == 0 and len(seen) >= 3
    xs = [x for x, _ in seen]
    assert xs == sorted(xs) and 0 < xs[0] < xs[-1] <= 0.99
    assert "встановлення" in seen[-1][1] and " з " in seen[-1][1]
    assert not os.path.exists(os.path.join(str(tmp_path), "downloads", "pip-tmp"))


def test_broken_new_copy_never_replaces_the_old_one(fake_component, monkeypatch):
    """The fresh copy is imported in a separate Python first; if that fails the old folder stays as it was."""
    os.makedirs(fake_component)
    with open(os.path.join(fake_component, "old_marker.txt"), "w") as f:
        f.write("old")
    monkeypatch.setattr(components, "_plan", lambda *a, **k: [])
    monkeypatch.setattr(components.downloader, "download_many", lambda *a, **k: None)

    def fake_pip(args, cancel=None, env=None):
        tmp = args[args.index("--target") + 1]
        os.makedirs(tmp, exist_ok=True)
        with open(os.path.join(tmp, "ddfake_pkg.py"), "w") as f:
            f.write("raise OSError('[WinError 127] The specified procedure could not be found')\n")
        return 0, []

    monkeypatch.setattr(components, "_pip", fake_pip)
    with pytest.raises(components.ComponentError) as e:
        components.ensure("fake")
    assert "WinError 127" in str(e.value) and "попередню залишено" in str(e.value)
    assert os.listdir(fake_component) == ["old_marker.txt"] and not os.path.exists(fake_component + ".new")


def test_swap_is_all_or_nothing(tmp_path, monkeypatch):
    target, new = str(tmp_path / "speakers"), str(tmp_path / "speakers.new")
    for d, f in ((target, "old.py"), (new, "new.py")):
        os.makedirs(d)
        open(os.path.join(d, f), "w").close()
    real_replace = os.replace

    def locked(src, dst):  # Windows: a folder whose DLLs are loaded cannot be moved
        if src == target:
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real_replace(src, dst)

    monkeypatch.setattr(components.os, "replace", locked)
    with pytest.raises(components.ComponentError) as e:
        components._swap(new, target, "Мовці")
    assert "Перезапустіть" in str(e.value)
    assert os.listdir(target) == ["old.py"] and not os.path.exists(new)  # nothing mixed
    monkeypatch.setattr(components.os, "replace", real_replace)
    os.makedirs(new)
    open(os.path.join(new, "new.py"), "w").close()
    components._swap(new, target, "Мовці")
    assert os.listdir(target) == ["new.py"] and not os.path.exists(target + ".old")


def test_plan_decodes_pytorch_index_file_names(tmp_path, monkeypatch):
    import json
    report = tmp_path / "plan.json"

    def fake_pip(args, cancel=None):
        assert "--extra-index-url" in args
        report.write_text(json.dumps({"install": [{"download_info": {
            "url": "https://download-r2.pytorch.org/whl/cu130/torch-2.11.0%2Bcu130-cp312-cp312-win_amd64.whl",
            "archive_info": {"hashes": {"sha256": "ab"}}}}]}), encoding="utf-8")
        return 0, []

    monkeypatch.setattr(components, "_pip", fake_pip)
    items = components._plan(["torch"], str(report), None, ["--extra-index-url", "https://x"])
    assert items[0]["name"] == "torch-2.11.0+cu130-cp312-cp312-win_amd64.whl" and items[0]["sha256"] == "ab"


@pytest.mark.parametrize("smi,expected", [
    ("| NVIDIA-SMI 621.40   KMD Version: 621.40   CUDA UMD Version: 13.5 |", True),
    ("| NVIDIA-SMI 581.15   Driver Version: 581.15   CUDA Version: 13.0 |", True),
    ("| NVIDIA-SMI 560.94   Driver Version: 560.94   CUDA Version: 12.6 |", False),
    ("", False)])
def test_gpu_torch_only_with_a_new_enough_driver(monkeypatch, smi, expected):
    import subprocess
    import types
    monkeypatch.setattr(components, "_cuda_cache", [])
    monkeypatch.setattr(components, "nvidia_gpu", lambda: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=smi))
    assert components.speakers_on_gpu() is expected
    packages, extra = components.pip_spec("speakers")
    if expected:
        assert "torch==2.11.0+cu130" in packages and extra == ["--extra-index-url", components.TORCH_CUDA["index"]]
    else:
        assert packages == components.COMPONENTS["speakers"]["pip"] and extra == []
    assert any(p.startswith("transformers") for p in packages)  # Parakeet runs through transformers
    assert components.pip_spec("whisper")[1] == []


def test_cpu_only_speakers_are_replaced_on_a_gpu_computer(tmp_path, monkeypatch):
    """Older installs got CPU-only torch from PyPI; with a suitable card the component is reinstalled
    (decided from the files, before torch is imported and its files get locked)."""
    monkeypatch.setenv("DD_ROOT", str(tmp_path))
    monkeypatch.setattr(components, "_ok", set())
    monkeypatch.setattr(components, "errors", {})
    monkeypatch.setattr(components, "_cuda_cache", [True])
    lib = os.path.join(components.component_dir("speakers"), "torch", "lib")
    os.makedirs(lib)
    open(os.path.join(lib, "torch_cpu.dll"), "w").close()
    monkeypatch.setattr(components.importlib, "import_module", lambda m: pytest.fail("torch must not be imported"))
    assert components.installed("speakers") is False and "процесора" in components.errors["speakers"]
    open(os.path.join(lib, "cublas64_13.dll"), "w").close()
    checked = []
    monkeypatch.setattr(components, "_verify", lambda name, folder, need_cuda=True: checked.append(folder) or "")
    assert components.installed("speakers") is True
    assert checked == [components.component_dir("speakers")], "torch is checked in a separate Python"


def test_broken_speakers_found_without_loading_them(tmp_path, monkeypatch):
    """A copy that fails to import (e.g. WinError 127) is reported as not installed, and since nothing of it
    was loaded into the app, ensure() may replace it right away."""
    monkeypatch.setenv("DD_ROOT", str(tmp_path))
    monkeypatch.setattr(components, "_ok", set())
    monkeypatch.setattr(components, "errors", {})
    monkeypatch.setattr(components, "_cuda_cache", [False])
    os.makedirs(components.component_dir("speakers"))
    monkeypatch.setattr(components, "_verify", lambda name, folder, need_cuda=True: "OSError: [WinError 127]")
    monkeypatch.setattr(components.importlib, "import_module", lambda m: pytest.fail("no in-process import"))
    assert components.installed("speakers") is False and "WinError 127" in components.errors["speakers"]
    assert not components._loaded_from(components.component_dir("speakers"))
    sys.path[:] = [p for p in sys.path if str(tmp_path) not in p]


def test_verified_speakers_are_not_imported_again_on_the_next_start(tmp_path, monkeypatch):
    """The isolated check imports torch (~10 s): once it passed for this copy it is remembered on disk,
    so opening «Транскрибувати» after a restart is instant; a changed copy is checked again."""
    monkeypatch.setenv("DD_ROOT", str(tmp_path))
    monkeypatch.setattr(components, "errors", {})
    monkeypatch.setattr(components, "_cuda_cache", [False])
    d = components.component_dir("speakers")
    os.makedirs(os.path.join(d, "torch-2.11.0.dist-info"))
    checked = []
    monkeypatch.setattr(components, "_verify", lambda name, folder, need_cuda=True: checked.append(folder) or "")
    for _ in range(2):  # two program starts
        monkeypatch.setattr(components, "_ok", set())
        assert components.installed("speakers") is True
    assert len(checked) == 1
    os.makedirs(os.path.join(d, "torch-2.12.0.dist-info"))  # another version installed
    monkeypatch.setattr(components, "_ok", set())
    assert components.installed("speakers") is True and len(checked) == 2


def test_status_does_not_measure_folders_unless_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("DD_ROOT", str(tmp_path))
    monkeypatch.setattr(components, "_ok", {"whisper", "speakers", "gpu"})
    monkeypatch.setattr(components, "_cuda_cache", [False])
    os.makedirs(components.component_dir("whisper"))
    monkeypatch.setattr(components, "folder_size", lambda p: pytest.fail("must not walk the folder"))
    assert components.status()["whisper"]["disk"] == ""
