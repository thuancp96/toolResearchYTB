from app.core.slideshow import (
    ImageEffect,
    Segment,
    build_slideshow_graph,
    resolve_image_effects,
)


def test_default_image_effect_preserves_static_branch():
    segment = Segment(0, "", 0, "", duration=2.0)
    graph = build_slideshow_graph([segment], 1280, 720, 30, [], 0.5, [])

    assert "scale=1280:720:force_original_aspect_ratio=increase" in graph
    assert "crop=1280:720,setsar=1,fps=30" in graph
    assert "eval=frame" not in graph


def test_zoom_and_move_emit_keyframe_filters():
    segment = Segment(0, "", 0, "", duration=2.0)
    zoom_graph = build_slideshow_graph(
        [segment], 1280, 720, 30, [], 0.5, [], [ImageEffect("zoom")], 1.3)
    move_graph = build_slideshow_graph(
        [segment], 1280, 720, 30, [], 0.5, [], [ImageEffect("move", "right")])

    assert "perspective=x0='(W-W/(1+(1.3000-1)*on/59))/2'" in zoom_graph
    assert "interpolation=cubic:eval=frame" in zoom_graph
    assert "scale=1472.000:828.000" in move_graph
    assert "192.000*t/2.0000" in move_graph


def test_move_and_random_effects_are_resolved_per_image():
    move = resolve_image_effects("move", 4)
    random_effects = resolve_image_effects("random", 12)

    assert len(move) == 4
    assert all(effect.kind == "move" for effect in move)
    assert {effect.direction for effect in move} <= {"left", "right", "up", "down"}
    assert {effect.kind for effect in random_effects} <= {"zoom", "move"}
