import dataclasses

import pytest

from ocfl_index.models import ObjectDetail, ObjectRow, PathRow, StatusReport, VersionRow


def _object() -> ObjectRow:
    return ObjectRow(
        object_id="alpha",
        root_relpath="al/ph/a/alpha",
        spec_version="1.1",
        digest_algorithm="sha256",
        head_version="v1",
        head_version_num=1,
        head_created_at="2024-01-01T00:00:00+00:00",
        inventory_digest="deadbeef",
        indexed_at="2024-01-02T00:00:00+00:00",
    )


def _version() -> VersionRow:
    return VersionRow(
        object_id="alpha",
        version_num=1,
        version="v1",
        created_at="2024-01-01T00:00:00+00:00",
        message=None,
        user_name=None,
        user_address=None,
    )


def _path() -> PathRow:
    return PathRow(
        object_id="alpha",
        version_num=1,
        logical_path="a.txt",
        digest="cafe",
    )


def test_status_report_is_frozen_slotted_with_fields() -> None:
    report = StatusReport(
        root_path="/r",
        schema_version="1",
        last_scan_at="2024-01-02T00:00:00+00:00",
        last_full_scan_at="2024-01-01T00:00:00+00:00",
        counts={"objects": 1, "versions": 1},
    )
    fields = {f.name for f in dataclasses.fields(report)}
    assert fields == {
        "root_path",
        "schema_version",
        "last_scan_at",
        "last_full_scan_at",
        "counts",
    }
    assert "__slots__" in StatusReport.__dict__
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(report, "root_path", "/other")  # noqa: B010


def test_object_detail_is_frozen_slotted_with_tuples() -> None:
    detail = ObjectDetail(
        object=_object(),
        versions=(_version(),),
        head_paths=(_path(),),
    )
    fields = {f.name for f in dataclasses.fields(detail)}
    assert fields == {"object", "versions", "head_paths"}
    assert "__slots__" in ObjectDetail.__dict__
    assert isinstance(detail.versions, tuple)
    assert isinstance(detail.head_paths, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(detail, "versions", ())  # noqa: B010
