def test_version_importable() -> None:
    from ocfl_index import __version__

    assert isinstance(__version__, str)


def test_package_has_a_docstring() -> None:
    import ocfl_index

    assert (ocfl_index.__doc__ or "").strip()


def test_every_export_has_its_own_docstring() -> None:
    import ocfl_index
    from tests.helpers.docs import undocumented_exports

    assert undocumented_exports(ocfl_index) == []
