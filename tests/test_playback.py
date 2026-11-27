"""Knowing whether Resolve plays (from playhead reports) so that Space is never pressed blindly."""

import types

from davinci_director.server import App


def app_with(moves, playhead):
    a = App.__new__(App)  # only the playback state is needed here
    a.ph_moves = list(moves)
    a.link = types.SimpleNamespace(playhead=playhead)
    return a


def test_playing_when_the_playhead_keeps_moving_forward():
    now = 100.0
    a = app_with([(now - 0.9, 1000), (now - 0.5, 1010), (now - 0.1, 1020)], 1020)
    assert a.playback(now) == {"playing": True, "frame": 1020}


def test_stopped_when_no_recent_moves():
    now = 100.0
    a = app_with([(now - 3.0, 1000), (now - 2.6, 1010), (now - 2.2, 1020)], 1020)
    assert a.playback(now)["playing"] is False


def test_a_single_jump_is_not_playback():
    now = 100.0
    assert app_with([(now - 0.2, 5000)], 5000).playback(now)["playing"] is False
    # clicking around (backwards / one jump after a long pause) is not playback either
    assert app_with([(now - 0.9, 1000), (now - 0.5, 900), (now - 0.1, 1200)], 1200).playback(now)["playing"] is False


def test_moves_are_recorded_from_engine_events():
    a = app_with([], None)
    a.emit = lambda *x, **k: None
    for f in range(8):
        a._on_engine("playhead", 1000 + f)
    assert len(a.ph_moves) == 6 and a.ph_moves[-1][1] == 1007
