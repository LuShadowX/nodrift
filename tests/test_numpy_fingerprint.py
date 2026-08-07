"""numpy.ndarray fingerprints (#1)."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from nodrift.fingerprint import fingerprint


def test_numpy_arrays_equal_with_float_noise():
    a = np.array([1.0, 2.0, 3.0], dtype=np.float64)
    b = a + 1e-15
    assert fingerprint(a) == fingerprint(b)


def test_numpy_shape_and_dtype_matter():
    a = np.zeros((2, 3), dtype=np.int32)
    b = np.zeros((3, 2), dtype=np.int32)
    assert fingerprint(a) != fingerprint(b)
