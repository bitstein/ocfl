"""Tests for layout protocol and registry."""

from typing import runtime_checkable

import pytest

from ocfl_lib.layout import Layout, LayoutRegistry, get_layout


class _DummyLayout:
    """Minimal Layout implementation for testing."""

    @property
    def name(self) -> str:
        return "dummy-layout"

    @property
    def config(self) -> dict | None:
        return None

    def resolve(self, object_id: str) -> str:
        return object_id


class TestLayoutProtocol:
    def test_protocol_is_runtime_checkable(self):
        assert runtime_checkable(Layout)

    def test_dummy_satisfies_protocol(self):
        layout = _DummyLayout()
        assert isinstance(layout, Layout)

    def test_resolve_returns_string(self):
        layout = _DummyLayout()
        assert layout.resolve("obj-1") == "obj-1"

    def test_name_property(self):
        layout = _DummyLayout()
        assert layout.name == "dummy-layout"


class TestLayoutRegistry:
    def test_register_and_get(self):
        reg = LayoutRegistry()
        reg.register("dummy-layout", lambda config: _DummyLayout())
        layout = reg.get_layout("dummy-layout")
        assert isinstance(layout, Layout)
        assert layout.name == "dummy-layout"

    def test_get_with_config(self):
        configs_seen: list[dict] = []

        def factory(config: dict | None = None) -> _DummyLayout:
            configs_seen.append(config)
            return _DummyLayout()

        reg = LayoutRegistry()
        reg.register("dummy-layout", factory)
        reg.get_layout("dummy-layout", config={"key": "val"})
        assert configs_seen == [{"key": "val"}]

    def test_unknown_layout_raises(self):
        import pytest

        reg = LayoutRegistry()
        with pytest.raises(KeyError, match="nonexistent"):
            reg.get_layout("nonexistent")

    def test_register_overwrites(self):
        reg = LayoutRegistry()
        reg.register("x", lambda config: _DummyLayout())
        reg.register("x", lambda config: _DummyLayout())
        # no error — last registration wins
        assert reg.get_layout("x") is not None


class TestDefaultRegistry:
    @pytest.mark.parametrize(
        "name",
        [
            "0002-flat-direct-storage-layout",
            "0003-hash-and-id-n-tuple-storage-layout",
            "0004-hashed-n-tuple-storage-layout",
            "0006-flat-omit-prefix-storage-layout",
            "0007-n-tuple-omit-prefix-storage-layout",
        ],
    )
    def test_builtin_layouts_registered(self, name):
        # 0006 requires config with delimiter
        if name == "0006-flat-omit-prefix-storage-layout":
            layout = get_layout(name, config={"delimiter": ":"})
        else:
            layout = get_layout(name)
        assert isinstance(layout, Layout)
        assert layout.name == name
