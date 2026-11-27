"""End-to-end: the real ``DaVinci Director.lua`` bridge (LuaJIT 2.1, sandboxed like
Resolve 21 free: no io/require/ffi/os.execute) driving the mock timeline through
the folder-name / file-name protocol used with Resolve."""

import os
import shutil
import threading
import time

import pytest

lupa = pytest.importorskip("lupa")
try:
    from lupa import luajit21 as lupa_rt
except ImportError:  # pragma: no cover
    lupa_rt = lupa

from davinci_director.engine import Engine, Transcript
from davinci_director.link import FileBridgeLink
from davinci_director.mock_resolve import MockResolve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LUA = os.path.join(ROOT, "resolve_scripts", "DaVinci Director.lua")


class Fusion:
    def __init__(self):
        self.data = {}

    def SetData(self, k, v):
        self.data[k] = v

    def GetData(self, k):
        return self.data.get(k)


CALLS = {"resolve": 0}  # Resolve API calls made by the script (each one goes through Resolve's UI queue)


def run_bridge(resolve, fusion, base, lock):
    lua = lupa_rt.LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()

    def to_py(v):
        if lupa_rt.lua_type(v) == "table":
            keys = list(v.keys())
            if keys and all(isinstance(k, int) for k in keys):
                return [to_py(v[i]) for i in range(1, len(keys) + 1)]
            return {k: to_py(x) for k, x in v.items()}
        if isinstance(v, Proxy):
            return v._o
        return v

    def to_lua(v):
        if isinstance(v, (list, tuple)):
            return lua.table_from([to_lua(x) for x in v])
        if isinstance(v, dict):
            return lua.table_from({k: to_lua(x) for k, x in v.items()})
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        return Proxy(v)

    class Proxy:
        def __init__(self, o):
            self._o = o

        def __getattr__(self, name):
            attr = getattr(self._o, name)
            if not callable(attr):
                return attr

            def call(*args):
                if args and isinstance(args[0], Proxy) and args[0]._o is self._o:
                    args = args[1:]
                if not isinstance(self._o, Fusion):
                    CALLS["resolve"] += 1
                with lock:
                    return to_lua(attr(*[to_py(a) for a in args]))
            return call

    g.resolve = Proxy(resolve)
    g.fusion = Proxy(fusion)
    def copyfile(a, b):  # like Resolve 21.2 free: implemented with io -> fails in the sandbox
        raise RuntimeError("attempt to index global 'io' (a nil value)")

    g.bmd = lua.table_from({"wait": lambda s: time.sleep(float(s)), "copyfile": copyfile,
                            "direxists": lambda p: os.path.isdir(p), "fileexists": lambda p: os.path.isfile(p)})
    lua.execute("io = nil; require = nil; package = nil; ffi = nil; os.execute = nil")
    with open(LUA, encoding="utf-8") as f:
        code = f.read().replace("__BRIDGE_DIR__", base)
    lua.execute(code)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DD_HOME", str(tmp_path / "home"))
    import tempfile
    base = tempfile.mkdtemp(prefix="ddb")  # short path, like %APPDATA%\DaVinciDirector\bridge
    r = MockResolve(fps=25.0)
    path = str(tmp_path / "Стрім запис.mp4")  # non-ASCII path on purpose
    mpi = r.add_media(path, fps=25.0, frames=1100)
    r.timeline_with(mpi, name="Стрім", ranges=[(0, 1100)])
    link = FileBridgeLink(base, poll=0.01).start()
    link.set_ui(True)  # the DaVinci Director window is open
    fusion, lock = Fusion(), threading.RLock()
    t = threading.Thread(target=run_bridge, args=(r, fusion, base, lock), daemon=True)
    t.start()
    deadline = time.time() + 10
    while link.snapshot is None and time.time() < deadline:
        time.sleep(0.02)
    assert link.snapshot is not None, "bridge did not report the timeline"
    yield r, link, path, lock
    try:
        link.run([{"op": "quit"}], timeout=5)
    except Exception:
        pass
    link.stop()
    t.join(5)


def wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_lua_bridge_roundtrip(setup, tmp_path):
    r, link, path, lock = setup
    assert link.connected
    snap = link.snapshot
    assert snap["tl"] == "Стрім"
    assert snap["check"] == []
    assert [(i["s"], i["e"]) for i in snap["tracks"]["video1"]] == [(90000, 91100)]
    assert snap["tracks"]["video1"][0]["path"] == path

    eng = Engine(link, state_dir=str(tmp_path / "projects"))
    eng.load_project("Stream Project")
    words = [[0.5 * i, 0.5 * i + 0.4, "слово%d" % i, 0, 1.0] for i in range(80)]
    eng.set_transcript(Transcript(path, words))
    assert eng.view["blocks"][0]["i1"] == 80

    # delete words 10..19 from the text -> real ripple delete on the timeline
    eng.delete({"a": {"b": 0, "w": 10}, "z": {"b": 0, "w": 19}})
    tl = r.project.current
    v = [(it.start, it.end) for it in tl.GetItemListInTrack("video", 1)]
    a = [(it.start, it.end) for it in tl.GetItemListInTrack("audio", 1)]
    assert len(v) == 2 and v[0][1] == v[1][0] and v == a
    blocks = eng.view["blocks"]
    assert [(b["i0"], b["i1"]) for b in blocks] == [(0, 10), (20, 80)]

    # move words 30..34 to the beginning
    eng.move({"a": {"b": 1, "w": 30}, "z": {"b": 1, "w": 34}}, {"b": 0, "w": None})
    assert eng.view["blocks"][0]["i0"] == 30 and eng.view["blocks"][0]["i1"] == 35

    # an edit made by the editor in Resolve is picked up by the watcher
    with lock:
        items = tl.GetItemListInTrack("video", 1)
        last = items[-1]
        tl.DeleteClips([last] + last.GetLinkedItems())
    assert wait_for(lambda: len(eng.view["blocks"]) == len(items) - 1)

    # click on a word -> playhead in Resolve
    eng.goto_word(0, 31)
    b = eng.view["blocks"][0]
    assert tl.playhead == b["s"] + round((15.5 - b["src0"]) * 25)  # word 31 starts at 15.5 s of the source
    assert wait_for(lambda: link.playhead == tl.playhead)

    # playhead moved in Resolve -> reported back
    with lock:
        tl.playhead = 90000 + 100
    assert wait_for(lambda: link.playhead == 90100)


def test_large_command_is_chunked(setup):
    r, link, path, lock = setup
    ops = [{"op": "marker", "f": i * 10, "name": "Сцена %d" % i, "note": "текст " * 20, "color": "Blue"}
           for i in range(30)]
    res = link.run(ops)
    assert res["ok"] and res["done"] == 30
    assert len(r.project.current.markers) == 30
    assert r.project.current.markers[0]["name"] == "Сцена 0"


def test_errors_are_reported(setup):
    r, link, path, lock = setup
    res = link.run([{"op": "delete", "refs": [{"t": "video", "i": 1, "s": 12345}]}])
    assert not res["ok"] and "not found" in res["err"]
    res = link.run([{"op": "nope"}])
    assert not res["ok"]


def test_mixed_rates_through_bridge(tmp_path, monkeypatch):
    """60 fps stream on a 24 fps timeline (the real-world case): source frames for
    AppendToTimeline must use the clip's own rate reported by the bridge."""
    import tempfile
    monkeypatch.setenv("DD_HOME", str(tmp_path / "home"))
    base = tempfile.mkdtemp(prefix="ddb")
    r = MockResolve(fps=24.0)
    path = str(tmp_path / "stream60.mp4")
    mpi = r.add_media(path, fps=60.0, frames=60 * 40)
    r.timeline_with(mpi, name="Mixed", ranges=[(0, 60 * 40)])
    link = FileBridgeLink(base, poll=0.01).start()
    link.set_ui(True)
    fusion, lock = Fusion(), threading.RLock()
    t = threading.Thread(target=run_bridge, args=(r, fusion, base, lock), daemon=True)
    t.start()
    assert wait_for(lambda: link.snapshot is not None, 10)
    assert link.snapshot["tracks"]["video1"][0]["mfps"] == 60.0
    eng = Engine(link, state_dir=str(tmp_path / "projects"))
    eng.load_project("P")
    words = [[0.5 * i, 0.5 * i + 0.4, "w%d" % i, 0, 1.0] for i in range(70)]
    eng.set_transcript(Transcript(path, words))
    eng.delete({"a": {"b": 0, "w": 10}, "z": {"b": 0, "w": 19}})
    blocks = eng.view["blocks"]
    assert [(b["i0"], b["i1"]) for b in blocks] == [(0, 10), (20, 70)]
    v = [(it.start, it.end, it.sf0) for it in r.project.current.GetItemListInTrack("video", 1)]
    assert v[0][1] == v[1][0], "no gap after ripple delete"
    assert abs(v[1][2] / 60.0 - blocks[1]["src0"]) < 0.05, "remainder starts at the right source time"
    eng.move({"a": {"b": 1, "w": 30}, "z": {"b": 1, "w": 34}}, {"b": 0, "w": None})
    assert (eng.view["blocks"][0]["i0"], eng.view["blocks"][0]["i1"]) == (30, 35)
    vv = r.project.current.GetItemListInTrack("video", 1)
    assert all(vv[i].end == vv[i + 1].start for i in range(len(vv) - 1))
    link.run([{"op": "quit"}], timeout=5)
    link.stop()


def test_idle_bridge_makes_no_resolve_calls(setup):
    """With the window closed the script must not call Resolve at all — every call
    is marshalled through Resolve's UI queue and makes the mouse cursor flicker."""
    r, link, path, lock = setup
    link.set_ui(False)
    time.sleep(0.7)  # let the current tick finish
    before = CALLS["resolve"]
    time.sleep(3.0)
    assert CALLS["resolve"] == before, "idle loop called the Resolve API %d times" % (CALLS["resolve"] - before)
    # a command still works while the window is closed (doorbell)
    res = link.run([{"op": "go", "tc": "01:00:02:00"}], timeout=10)
    assert res["ok"] and r.project.current.playhead == 90000 + 50
    # window open: watching resumes, but only a few calls per second
    link.set_ui(True)
    time.sleep(0.7)
    before = CALLS["resolve"]
    time.sleep(2.0)
    per_sec = (CALLS["resolve"] - before) / 2.0
    assert per_sec < 40, "too many Resolve calls while watching: %.0f/s" % per_sec
