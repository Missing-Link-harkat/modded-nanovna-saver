"""Frame plan of the segmented sweep (litevna-openvna#204), no Qt."""

import pytest

from NanoVNASaver.SegmentedSweep import plan_frames


def grid(plan):
    return [f for fr in plan.frames for f in fr.frequencies()]


def test_points_are_exactly_on_the_grid_without_shared_edges():
    plan = plan_frames(23_500_000, 100_000_000, 100_000, max_points=401)
    freqs = grid(plan)
    assert plan.points == (100_000_000 - 23_500_000) // 100_000 + 1 == 766
    assert freqs == [23_500_000 + k * 100_000 for k in range(766)]
    assert len(set(freqs)) == len(freqs)
    assert plan.stop_hz == 100_000_000


def test_frames_are_balanced_and_within_device_limits():
    plan = plan_frames(1_000_000, 1_000_000 + 401 * 1000, 1000, max_points=401)
    sizes = [f.points for f in plan.frames]
    assert plan.points == 402
    assert sizes == [201, 201]  # not 401 + 1
    for n in (11, 400, 401, 402, 801, 802, 30_001):
        plan = plan_frames(50_000_000, 50_000_000 + (n - 1) * 10, 10,
                           max_points=401)
        sizes = [f.points for f in plan.frames]
        assert sum(sizes) == n
        assert max(sizes) <= 401 and min(sizes) >= 11
        assert max(sizes) - min(sizes) <= 1


def test_stop_off_grid_is_rounded_down():
    plan = plan_frames(10_000_000, 10_001_950, 100, max_points=401)
    assert plan.stop_hz == 10_001_900
    assert plan.points == 20


def test_frame_start_and_stop():
    plan = plan_frames(100_000_000, 200_000_000, 50_000, max_points=401)
    for a, b in zip(plan.frames, plan.frames[1:]):
        assert b.start_hz == a.stop_hz + plan.step_hz
        assert b.first == a.first + a.points


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((1_000_000, 2_000_000, 0), "Askeleen"),
        ((2_000_000, 1_000_000, 1000), "Alkutaajuuden"),
        ((1_000_000, 1_000_500, 100), "vain 6 pistettä"),
    ],
)
def test_refusals_say_why(args, message):
    with pytest.raises(ValueError, match=message):
        plan_frames(*args, max_points=401)


def test_a_plan_of_too_many_points_is_refused_before_any_frame_is_built():
    # MHz typed where Hz was meant: 2.35e13 Hz .. 1e14 Hz at 1 kHz = 7.6e10 points.
    # This used to build ~1.9e8 Frame objects (the CI runner ran out of memory).
    with pytest.raises(ValueError, match="enintään 1000000"):
        plan_frames(23_500_000 * 10**6, 100_000_000 * 10**6, 1000, max_points=401)
    with pytest.raises(ValueError, match="pistettä"):
        plan_frames(1, 10**12, 1, max_points=401)


def test_the_point_limit_itself_is_allowed():
    from NanoVNASaver.SegmentedSweep import MAX_TOTAL_POINTS
    plan = plan_frames(1_000_000, 1_000_000 + (MAX_TOTAL_POINTS - 1) * 10, 10,
                       max_points=401)
    assert plan.points == MAX_TOTAL_POINTS
    with pytest.raises(ValueError):
        plan_frames(1_000_000, 1_000_000 + MAX_TOTAL_POINTS * 10, 10, max_points=401)
