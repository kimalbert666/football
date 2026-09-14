import json

import f2_fusion as ff


def test_combine_weight_zero_preserves_f3():
    probs = [0.55, 0.25, 0.20]
    assert ff.combine(probs, 2, 0.0) == probs


def test_combine_boosts_only_f2_direction():
    probs = [0.45, 0.30, 0.25]
    fused = ff.combine(probs, 2, 0.5)
    assert abs(sum(fused) - 1.0) < 1e-12
    assert fused[2] > probs[2]
    assert fused[0] < probs[0]


def test_calibrate_keeps_zero_below_minimum(tmp_path, monkeypatch):
    config = tmp_path / "f2_fusion.json"
    monkeypatch.setattr(ff, "CONFIG", config)
    monkeypatch.setattr(ff, "load_pairs", lambda: [])
    assert ff.calibrate() is False
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["weight"] == 0.0
    assert saved["status"] == "collecting"
