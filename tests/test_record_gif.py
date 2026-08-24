"""Registry integrity for scripts/record_gif.py -- no Pilot, no rendering,
just the SCENES table itself: names are unique and non-empty, every scene
declares more than one frame (a GIF that can only ever have one frame is a
still wearing a GIF extension, which is a bug in the registry, not
something worth spending an inkscape+Pillow run to discover), and every
widget class a scene claims to exercise really is a class object -- catches
a scene left pointing at a typo'd string, or one that quietly stopped
importing, without anyone noticing until the gallery script itself blew up
loudly (or, worse, silently rendered nothing new).

Actually invoking scenes.record_gif's scenes (Pilot + inkscape + Pillow) is
the OTHER half of this item's test coverage -- `uv run python
scripts/record_gif.py` itself, which this file deliberately does not
duplicate: the scenes running to completion and producing a byte size
under budget IS the test, same footing as scripts/screenshot.py's own
scenes.
"""
from __future__ import annotations

from scripts import record_gif


def test_scene_names_are_unique_and_nonempty():
    names = [scene.name for scene in record_gif.SCENES]
    assert names, "no scenes registered"
    assert all(names), "a scene has an empty name"
    assert len(names) == len(set(names)), "duplicate scene name"


def test_every_scene_declares_more_than_one_frame():
    for scene in record_gif.SCENES:
        assert scene.min_frames > 1, (
            f"scene {scene.name!r} declares min_frames={scene.min_frames} "
            f"-- a GIF needs at least 2 frames to be a GIF"
        )


def test_every_scene_references_real_widget_classes():
    for scene in record_gif.SCENES:
        assert scene.widgets, f"scene {scene.name!r} declares no widgets"
        for widget in scene.widgets:
            assert isinstance(widget, type), (
                f"scene {scene.name!r} has a non-class entry in widgets: "
                f"{widget!r}"
            )


def test_every_scene_has_a_size_and_an_engine_factory():
    for scene in record_gif.SCENES:
        cols, rows = scene.size
        assert cols > 0 and rows > 0, scene.name
        assert scene.engine_factory is not None, (
            f"scene {scene.name!r} has no engine_factory"
        )


def test_scene_sizes_stay_within_two_percent_of_16_9():
    """The same geometry constants scripts/screenshot.py calibrated
    (width = 12.2*cols + 18, height = 24.375*rows + 51) -- every scene here
    reuses one of screenshot.py's own already-vetted sizes rather than
    deriving a new one, so this is a guard against a future scene picking
    an untested size, not a re-derivation."""
    target = 16 / 9
    for scene in record_gif.SCENES:
        cols, rows = scene.size
        width = 12.2 * cols + 18
        height = 24.375 * rows + 51
        ratio = width / height
        assert abs(ratio - target) / target <= 0.02, (
            f"scene {scene.name!r} size {scene.size} -> ratio {ratio:.3f}, "
            f"more than 2% off 16:9 ({target:.3f})"
        )


def test_scene_names_match_their_gif_filenames():
    """Names double as `assets/shots/<name>.gif` -- lowercase, hyphenated,
    no path-hostile characters, same convention scripts/screenshot.py's
    scene names already follow for `<name>.svg`."""
    for scene in record_gif.SCENES:
        assert scene.name == scene.name.lower(), scene.name
        assert " " not in scene.name, scene.name
        assert "/" not in scene.name, scene.name
