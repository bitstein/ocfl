"""Tests for core types: Spec, VNum, Namaste."""

import pytest

from ocfl_lib._types import (
    DEFAULT_CONTENT_DIRECTORY,
    DEFAULT_DIGEST_ALGORITHM,
    DEFAULT_SPEC,
    INVENTORY_FILENAME,
    Spec,
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
