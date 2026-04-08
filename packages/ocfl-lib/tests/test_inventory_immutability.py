"""Frozen-attr enforcement on Inventory structs."""

import pytest

from ocfl_lib.inventory import Inventory, UserInfo, VersionInfo


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
    with pytest.raises(AttributeError):
        inv.head = "v2"
    with pytest.raises(AttributeError):
        inv.manifest = {}
    with pytest.raises(AttributeError):
        inv.versions = {}


def test_version_info_field_reassign_blocked():
    vi = VersionInfo(created="2024-01-01T00:00:00Z", state={})
    with pytest.raises(AttributeError):
        vi.created = "x"
    with pytest.raises(AttributeError):
        vi.state = {}


def test_user_info_field_reassign_blocked():
    u = UserInfo(name="alice")
    with pytest.raises(AttributeError):
        u.name = "bob"
    with pytest.raises(AttributeError):
        u.address = "x"
