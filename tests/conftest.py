import os, sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _own_program_folder(tmp_path_factory, monkeypatch):
    """No test may touch the real program folder (%APPDATA%\\Blackmagic Design\\...\\DaVinci Director):
    by default everything goes into a temporary one; a test can still point DD_ROOT / DD_HOME elsewhere."""
    if not os.environ.get("DD_ROOT"):
        monkeypatch.setenv("DD_ROOT", str(tmp_path_factory.mktemp("ddroot")))
