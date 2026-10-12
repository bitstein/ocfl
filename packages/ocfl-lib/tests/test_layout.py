"""Tests for layout protocol and registry."""

import pytest

from ocfl_lib.errors import UnknownLayout
from ocfl_lib.layout import DEFAULT_LAYOUTS, Layout, LayoutRegistry, get_layout


class _DummyLayout(Layout):
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
    def test_resolve_returns_string(self):
        layout = _DummyLayout()
        assert layout.resolve("obj-1") == "obj-1"

    def test_name_property(self):
        layout = _DummyLayout()
        assert layout.name == "dummy-layout"


class TestLayoutRegistry:
    def test_with_layout_and_get(self):
        reg = LayoutRegistry().with_layout(
            "dummy-layout", lambda config: _DummyLayout()
        )
        layout = reg.get_layout("dummy-layout")
        assert layout.name == "dummy-layout"

    def test_get_with_config(self):
        configs_seen: list[dict | None] = []

        def factory(config: dict | None = None) -> _DummyLayout:
            configs_seen.append(config)
            return _DummyLayout()

        reg = LayoutRegistry({"dummy-layout": factory})
        reg.get_layout("dummy-layout", config={"key": "val"})
        assert configs_seen == [{"key": "val"}]

    def test_unknown_layout_raises(self):
        reg = LayoutRegistry()
        with pytest.raises(UnknownLayout, match="nonexistent"):
            reg.get_layout("nonexistent")
        with pytest.raises(KeyError):
            reg.get_layout("nonexistent")

    def test_with_layout_returns_new_registry(self):
        base = LayoutRegistry()
        reg = base.with_layout("x", lambda config: _DummyLayout())
        assert "x" in reg
        assert "x" not in base

    def test_with_layout_overrides(self):
        first, second = _DummyLayout(), _DummyLayout()
        reg = LayoutRegistry({"x": lambda config: first})
        reg = reg.with_layout("x", lambda config: second)
        assert reg.get_layout("x") is second

    def test_immutable(self):
        with pytest.raises(AttributeError):
            DEFAULT_LAYOUTS._factories = {}  # ty: ignore[invalid-assignment]
        with pytest.raises(TypeError):
            DEFAULT_LAYOUTS._factories["x"] = lambda config: _DummyLayout()

    def test_get_layout_uses_explicit_registry(self):
        reg = LayoutRegistry({"x": lambda config: _DummyLayout()})
        assert get_layout("x", layouts=reg).name == "dummy-layout"
        with pytest.raises(UnknownLayout):
            get_layout("x")


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
        assert layout.name == name
