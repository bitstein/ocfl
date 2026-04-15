def test_version_importable() -> None:
    from ocfl_index import __version__

    assert isinstance(__version__, str)
    assert __version__
