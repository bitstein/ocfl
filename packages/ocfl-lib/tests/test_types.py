"""Tests for core types: Spec, VNum, Namaste."""

import pytest

from ocfl_lib.spec import (
    DEFAULT_CONTENT_DIRECTORY,
    DEFAULT_DIGEST_ALGORITHM,
    DEFAULT_SPEC,
    INVENTORY_FILENAME,
    Namaste,
    Spec,
    VNum,
)


class TestSpec:
    def test_values(self):
        assert Spec.V1_0.value == "1.0"
        assert Spec.V1_1.value == "1.1"

    def test_str(self):
        assert str(Spec.V1_1) == "1.1"
        assert str(Spec.V1_0) == "1.0"

    def test_inventory_type_url(self):
        assert Spec.V1_1.inventory_type_url == "https://ocfl.io/1.1/spec/#inventory"
        assert Spec.V1_0.inventory_type_url == "https://ocfl.io/1.0/spec/#inventory"

    def test_from_inventory_type(self):
        assert (
            Spec.from_inventory_type("https://ocfl.io/1.0/spec/#inventory") == Spec.V1_0
        )
        assert (
            Spec.from_inventory_type("https://ocfl.io/1.1/spec/#inventory") == Spec.V1_1
        )

    def test_from_inventory_type_unknown(self):
        with pytest.raises(ValueError):
            Spec.from_inventory_type("https://ocfl.io/2.0/spec/#inventory")

    def test_from_inventory_type_malformed(self):
        with pytest.raises(ValueError):
            Spec.from_inventory_type("not-a-url")


class TestConstants:
    def test_default_spec(self):
        assert DEFAULT_SPEC == Spec.V1_1

    def test_inventory_filename(self):
        assert INVENTORY_FILENAME == "inventory.json"

    def test_default_content_directory(self):
        assert DEFAULT_CONTENT_DIRECTORY == "content"

    def test_default_digest_algorithm(self):
        assert DEFAULT_DIGEST_ALGORITHM == "sha512"


class TestVNumParse:
    @pytest.mark.parametrize(
        ("s", "number", "padding"),
        [
            ("v1", 1, 0),
            ("v2", 2, 0),
            ("v100", 100, 0),
            ("v001", 1, 3),
            ("v099", 99, 3),
        ],
    )
    def test_parse_valid(self, s: str, number: int, padding: int):
        vn = VNum.parse(s)
        assert vn.number == number
        assert vn.padding == padding

    @pytest.mark.parametrize("s", ["v0", "v00", "v", "v-1", "1", "", "v1.0"])
    def test_parse_invalid(self, s: str):
        with pytest.raises(ValueError):
            VNum.parse(s)


class TestVNumStr:
    @pytest.mark.parametrize("s", ["v1", "v2", "v001", "v099", "v100", "v00042"])
    def test_round_trip(self, s: str):
        assert str(VNum.parse(s)) == s

    def test_no_padding(self):
        assert str(VNum(1, 0)) == "v1"

    def test_with_padding(self):
        assert str(VNum(1, 3)) == "v001"

    @pytest.mark.parametrize("number", [100, 999])
    def test_rejects_unrepresentable_padded_value(self, number: int):
        with pytest.raises(ValueError, match="overflows padding"):
            VNum(number, 3)


class TestVNumOrdering:
    def test_lt(self):
        assert VNum(1) < VNum(2)
        assert not VNum(2) < VNum(1)

    def test_eq_ignores_padding(self):
        assert VNum(1, 0) == VNum(1, 3)

    def test_hash_ignores_padding(self):
        assert hash(VNum(1, 0)) == hash(VNum(1, 3))

    def test_gt(self):
        assert VNum(2) > VNum(1)

    def test_le(self):
        assert VNum(1) <= VNum(2)
        assert VNum(1) <= VNum(1)

    def test_ge(self):
        assert VNum(2) >= VNum(1)
        assert VNum(2) >= VNum(2)

    def test_set_dedup(self):
        assert len({VNum(1, 0), VNum(1, 3)}) == 1


class TestVNumNext:
    def test_next(self):
        assert VNum.parse("v1").next() == VNum(2, 0)

    def test_next_preserves_padding(self):
        assert VNum.parse("v001").next() == VNum(2, 3)
        assert str(VNum.parse("v001").next()) == "v002"

    def test_next_overflow(self):
        with pytest.raises(ValueError):
            VNum(99, 3).next()

    def test_next_at_padded_boundary(self):
        vn = VNum(98, 3).next()
        assert vn == VNum(99, 3)
        assert str(vn) == "v099"

    def test_next_no_overflow_without_padding(self):
        assert VNum(999, 0).next() == VNum(1000, 0)


class TestVNumFirst:
    def test_first_no_padding(self):
        assert VNum.first() == VNum(1, 0)

    def test_first_with_padding(self):
        assert VNum.first(3) == VNum(1, 3)
        assert str(VNum.first(3)) == "v001"


class TestNamaste:
    def test_root_1_0(self):
        n = Namaste.for_root(Spec.V1_0)
        assert n.filename == "0=ocfl_1.0"
        assert n.content == "ocfl_1.0\n"

    def test_root_1_1(self):
        n = Namaste.for_root(Spec.V1_1)
        assert n.filename == "0=ocfl_1.1"
        assert n.content == "ocfl_1.1\n"

    def test_object_1_0(self):
        n = Namaste.for_object(Spec.V1_0)
        assert n.filename == "0=ocfl_object_1.0"
        assert n.content == "ocfl_object_1.0\n"

    def test_object_1_1(self):
        n = Namaste.for_object(Spec.V1_1)
        assert n.filename == "0=ocfl_object_1.1"
        assert n.content == "ocfl_object_1.1\n"

    @pytest.mark.parametrize(
        "filename",
        [
            "0=ocfl_1.0",
            "0=ocfl_1.1",
            "0=ocfl_object_1.0",
            "0=ocfl_object_1.1",
        ],
    )
    def test_parse_round_trip(self, filename: str):
        n = Namaste.parse(filename)
        assert n is not None
        assert n.filename == filename

    @pytest.mark.parametrize(
        "filename",
        ["1=ocfl_1.0", "0=ocfl_2.0", "0=foo_1.1", "readme.txt", ""],
    )
    def test_parse_invalid(self, filename: str):
        assert Namaste.parse(filename) is None
