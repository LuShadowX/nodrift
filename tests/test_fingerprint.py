"""The fingerprint is the whole comparison. If it is unstable, everything
downstream is noise, so these tests pin the properties it must have."""

from __future__ import annotations

import pytest

from nodrift.fingerprint import fingerprint


def test_primitives_round_trip():
    assert fingerprint(1) == fingerprint(1)
    assert fingerprint("a") != fingerprint("b")
    assert fingerprint(1) != fingerprint(True)


def test_float_specials_are_representable():
    assert fingerprint(float("nan")) == fingerprint(float("nan"))
    assert fingerprint(float("inf")) != fingerprint(float("-inf"))
    assert fingerprint(0.1 + 0.2) != fingerprint(0.3)


def test_set_order_does_not_matter():
    assert fingerprint({1, 2, 3}) == fingerprint({3, 1, 2})


def test_dict_insertion_order_does_not_matter():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_memory_addresses_are_scrubbed():
    class Opaque:
        __slots__ = ()

    # Two distinct instances differ only by address; they must compare equal.
    assert fingerprint(repr(Opaque())) == fingerprint(repr(Opaque()))


def test_addresses_inside_strings_are_scrubbed():
    a = "failed on <object object at 0x10cca3740>"
    b = "failed on <object object at 0x1086bb740>"
    assert fingerprint(a) == fingerprint(b)


def test_exceptions_compare_by_type_and_message():
    assert fingerprint(ValueError("x")) == fingerprint(ValueError("x"))
    assert fingerprint(ValueError("x")) != fingerprint(ValueError("y"))
    assert fingerprint(ValueError("x")) != fingerprint(TypeError("x"))


def test_objects_compare_by_state_not_identity():
    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y

    assert fingerprint(Point(1, 2)) == fingerprint(Point(1, 2))
    assert fingerprint(Point(1, 2)) != fingerprint(Point(1, 3))


def test_slots_are_included():
    class Slotted:
        __slots__ = ("a",)

        def __init__(self, a):
            self.a = a

    assert fingerprint(Slotted(1)) == fingerprint(Slotted(1))
    assert fingerprint(Slotted(1)) != fingerprint(Slotted(2))


def test_cycles_terminate():
    a: list = [1]
    a.append(a)
    b: list = [1]
    b.append(b)
    assert fingerprint(a) == fingerprint(b)


def test_deep_nesting_terminates():
    deep: object = 0
    for _ in range(200):
        deep = [deep]
    assert fingerprint(deep) == fingerprint(deep)


@pytest.mark.parametrize("value", [b"\x00\xff", (1, 2), [1, 2], frozenset({1})])
def test_containers_are_stable(value):
    assert fingerprint(value) == fingerprint(value)


def test_list_and_tuple_are_distinguishable():
    assert fingerprint([1, 2]) != fingerprint((1, 2))


def test_sampling_still_records_novel_inputs_after_long_repetition():
    """Adaptive back-off must not blind a function to new inputs.

    A hot function called thousands of times with the same value backs off to
    sampling one call in many. If a genuinely new input then arrives, it still
    has to be recorded — otherwise speed was bought with coverage.
    """
    from nodrift.recorder import Recorder

    recorder = Recorder(["_none_"], max_per_target=600)

    for _ in range(5000):
        recorder._capture("m:f", ("same",), {})
    assert len(recorder.calls["m:f"]) == 1
    assert recorder.stats["sampled_out"] > 0, "back-off never engaged"

    # A new value, repeated enough to survive whatever skip factor is active.
    for _ in range(2000):
        recorder._capture("m:f", ("different",), {})
    assert len(recorder.calls["m:f"]) == 2, "novel input was missed"


def test_sampling_resets_when_a_function_stays_productive():
    from nodrift.recorder import Recorder

    recorder = Recorder(["_none_"], max_per_target=600)
    for i in range(200):
        recorder._capture("m:g", (i,), {})
    # Every call was novel, so back-off should never have engaged.
    assert len(recorder.calls["m:g"]) == 200
    assert recorder.stats.get("sampled_out", 0) == 0


def test_the_cap_holds_when_threads_record_at_once():
    """A threaded suite must not be able to record past max_per_target.

    Every input here is novel, so nothing is deduplicated away and the cap is
    the only thing standing between eight threads and an oversized recording.
    """
    import threading

    from nodrift.recorder import Recorder

    cap = 50
    recorder = Recorder(["_none_"], max_per_target=cap)
    threads = [
        threading.Thread(
            target=lambda w=worker: [
                recorder._capture("m:h", (w, i), {}) for i in range(200)
            ]
        )
        for worker in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(recorder.calls["m:h"]) == cap, "cap exceeded under concurrency"


def test_stats_add_up_when_threads_record_at_once():
    """Lost counter increments would make the reported numbers untrue."""
    import threading

    from nodrift.recorder import Recorder

    calls_per_thread, workers = 200, 8
    recorder = Recorder(["_none_"], max_per_target=10_000)
    threads = [
        threading.Thread(
            target=lambda w=worker: [
                recorder._capture("m:i", (w, i), {}) for i in range(calls_per_thread)
            ]
        )
        for worker in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    total = calls_per_thread * workers
    accounted = (
        recorder.stats["captured"]
        + recorder.stats["sampled_out"]
        + recorder.stats["skipped_at_cap"]
        + recorder.stats["unpicklable"]
        + recorder.stats["oversized"]
    )
    assert accounted == total, f"{total - accounted} calls unaccounted for"
    assert len(recorder.calls["m:i"]) == recorder.stats["captured"]


# --------------------------------------------------------------------------
# numpy arrays
# --------------------------------------------------------------------------

def test_numpy_is_not_imported_for_projects_that_do_not_use_it():
    """Looking numpy up in sys.modules must never trigger an import.

    A project that has nothing to do with numpy should not pay ~100ms and the
    memory to load it because the fingerprinter went looking.
    """
    import sys

    real = sys.modules.pop("numpy", None)
    try:
        fingerprint({"a": [1, 2.5, "x"], "b": (None, True)})
        assert "numpy" not in sys.modules, "fingerprint imported numpy"
    finally:
        if real is not None:
            sys.modules["numpy"] = real


def test_numpy_shape_and_dtype_are_part_of_the_answer():
    np = pytest.importorskip("numpy")

    assert fingerprint(np.zeros((2, 3))) != fingerprint(np.zeros((3, 2)))
    assert fingerprint(np.zeros(3, dtype="int32")) != fingerprint(
        np.zeros(3, dtype="int64")
    )
    assert fingerprint(np.arange(6)) == fingerprint(np.arange(6))


def test_numpy_floats_are_compared_the_same_way_scalars_are():
    """One tolerance question, one answer, whatever the container.

    A rounding-based tolerance would give a different verdict for a float in
    an array than for the same float on its own, and the size of that
    tolerance would depend on the magnitude of the value.
    """
    np = pytest.importorskip("numpy")

    for magnitude in (1.0, 1e6, 1e12, 1e18):
        noisy = magnitude * (1 + 1e-15)
        scalars_agree = fingerprint(magnitude) == fingerprint(noisy)
        arrays_agree = fingerprint(np.array([magnitude])) == fingerprint(
            np.array([noisy])
        )
        assert scalars_agree == arrays_agree, f"disagreed at {magnitude:g}"


def test_numpy_nan_does_not_depend_on_an_identity_shortcut():
    """`nan != nan`, so returning raw floats makes equality accidental.

    Naming nan the way the scalar path does keeps the answer stable through
    the JSON round trip the comparison actually performs.
    """
    import json

    np = pytest.importorskip("numpy")

    a = fingerprint(np.array([np.nan, np.inf, -np.inf]))
    b = fingerprint(np.array([np.nan, np.inf, -np.inf]))
    assert a == b, "nan arrays disagree in process"
    assert json.loads(json.dumps(a)) == json.loads(json.dumps(b))
    assert a[3] == [["float", "nan"], ["float", "inf"], ["float", "-inf"]], a


def test_numpy_object_arrays_recurse():
    np = pytest.importorskip("numpy")

    a = np.array([{"k": 1}, [2, 3]], dtype=object)
    b = np.array([{"k": 1}, [2, 3]], dtype=object)
    c = np.array([{"k": 2}, [2, 3]], dtype=object)
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(c)
