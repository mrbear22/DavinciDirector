"""One program folder for everything, and moving data of older versions into it."""

import os

from davinci_director import components, storage


def test_everything_under_one_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DD_ROOT", str(tmp_path / "root"))
    monkeypatch.delenv("DD_HOME", raising=False)
    for v in ("HF_HOME", "MPLCONFIGDIR", "TORCH_HOME", "PIP_NO_CACHE_DIR"):
        monkeypatch.delenv(v, raising=False)
    storage.setup_env()
    r = str(tmp_path / "root")
    assert storage.base_dir() == os.path.join(r, "data")
    assert components.data_dir() == r and components.component_dir("whisper").startswith(r)
    for v in ("HF_HOME", "MPLCONFIGDIR", "TORCH_HOME"):
        assert os.environ[v].startswith(r), v
    assert os.environ["PIP_NO_CACHE_DIR"] == "1"


def test_installed_program_uses_the_folder_it_was_installed_into(tmp_path, monkeypatch):
    chosen = tmp_path / "D-drive" / "Мої програми" / "DaVinci Director"
    (chosen / "python").mkdir(parents=True)
    (chosen / "python" / "python.exe").write_bytes(b"")
    pkg = chosen / "app" / "davinci_director"
    pkg.mkdir(parents=True)
    assert storage.installed_root(str(pkg)) == str(chosen)
    assert storage.installed_root(str(tmp_path / "src" / "davinci_director")) is None  # source checkout
    monkeypatch.delenv("DD_ROOT", raising=False)
    monkeypatch.setattr(storage, "installed_root", lambda pkg_dir=None: str(chosen))
    assert storage.root() == str(chosen)


def test_default_root_is_inside_resolves_user_folder(monkeypatch):
    monkeypatch.setattr(storage, "installed_root", lambda pkg_dir=None: None)
    monkeypatch.delenv("DD_ROOT", raising=False)
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", r"C:\Users\u\AppData\Roaming")
        assert storage.root() == r"C:\Users\u\AppData\Roaming\Blackmagic Design\DaVinci Resolve\Support\DaVinci Director"


def test_old_locations_are_moved_once(tmp_path, monkeypatch):
    home, local, appdata = tmp_path / "home", tmp_path / "local", tmp_path / "roaming"
    (home / ".davinci_director" / "transcripts").mkdir(parents=True)
    (home / ".davinci_director" / "settings.json").write_text("{}")
    for rel in ("components/speakers/torch", "components/gpu/nvidia", "models/ihnatov-yuri--whisper-large-v3-turbo-uk",
                "models/ihnatov-yuri--parakeet-tdt-0.6b-v3-uk"):
        (local / "DaVinciDirector" / rel).mkdir(parents=True)
    hf = home / ".cache" / "huggingface" / "hub"
    (hf / "models--pyannote--speaker-diarization-community-1").mkdir(parents=True)
    (hf / "models--someone--else").mkdir(parents=True)  # not ours: stays
    monkeypatch.delenv("DD_ROOT", raising=False)
    monkeypatch.delenv("DD_HOME", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(home)))
    monkeypatch.setattr(storage, "installed_root", lambda pkg_dir=None: None)
    storage.setup_env()
    r = storage.root()
    assert os.path.isfile(os.path.join(r, "data", "settings.json"))
    assert os.path.isdir(os.path.join(r, "components", "speakers", "torch"))
    assert os.path.isdir(os.path.join(r, "models", "ihnatov-yuri--parakeet-tdt-0.6b-v3-uk"))
    assert not os.path.exists(os.path.join(r, "components", "gpu"))  # obsolete: removed, not moved
    assert not os.path.exists(os.path.join(r, "models", "ihnatov-yuri--whisper-large-v3-turbo-uk"))
    assert os.path.isdir(os.path.join(r, "hf", "hub", "models--pyannote--speaker-diarization-community-1"))
    assert (hf / "models--someone--else").is_dir()
    assert not (home / ".davinci_director").exists() and not (local / "DaVinciDirector").exists()
    storage.setup_env()  # second start: nothing to do
    assert os.path.isfile(os.path.join(r, "data", ".migrated"))
