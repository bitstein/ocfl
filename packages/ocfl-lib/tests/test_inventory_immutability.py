"""Frozen-attr enforcement on Inventory structs."""

import pytest

from ocfl_lib._inventory import Inventory, UserInfo, VersionInfo


def _inv() -> Inventory:
    return Inventory(
        id="http://example.org/o",
        type="https://ocfl.io/1.1/spec/#inventory",
        digest_algorithm="sha512",
        head="v1",
        manifest={"abc": ["v1/content/f.txt"]},
        versions={
            "v1": VersionInfo(
                created="2024-01-01T00:00:00Z",
                state={"abc": ["f.txt"]},
                user=UserInfo(name="alice"),
            )
        },
    )


def test_inventory_field_reassign_blocked():
    inv = _inv()
    for field, value in [("head", "v2"), ("manifest", {}), ("versions", {})]:
        with pytest.raises(AttributeError):
            setattr(inv, field, value)


def test_version_info_field_reassign_blocked():
    vi = VersionInfo(created="2024-01-01T00:00:00Z", state={})
    for field, value in [("created", "x"), ("state", {})]:
        with pytest.raises(AttributeError):
            setattr(vi, field, value)


def test_user_info_field_reassign_blocked():
    u = UserInfo(name="alice")
    for field, value in [("name", "bob"), ("address", "x")]:
        with pytest.raises(AttributeError):
            setattr(u, field, value)
