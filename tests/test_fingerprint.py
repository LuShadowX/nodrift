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
