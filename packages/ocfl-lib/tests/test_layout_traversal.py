import pytest

from ocfl_lib.layout.ext0002_flat_direct import FlatDirectLayout
from ocfl_lib.layout.ext0006_flat_omit_prefix import FlatOmitPrefixLayout
from ocfl_lib.layout.ext0007_n_tuple_omit_prefix import NTupleOmitPrefixLayout


@pytest.fixture
def flat_omit():
    return FlatOmitPrefixLayout(delimiter=":")


@pytest.mark.parametrize(
    "oid",
    ["foo:", "foo:.", "foo:..", "foo:../etc", "foo:bar/baz", "foo:\x00"],
)
def test_flat_omit_rejects(flat_omit, oid):
    with pytest.raises(ValueError):
        flat_omit.resolve(oid)


def test_flat_omit_happy(flat_omit):
    assert flat_omit.resolve("foo:bar") == "bar"


@pytest.fixture
def ntuple_omit():
    return NTupleOmitPrefixLayout()


@pytest.mark.parametrize("oid", ["foo:", "foo:..", "foo:bar/baz"])
def test_ntuple_omit_rejects(ntuple_omit, oid):
    with pytest.raises(ValueError):
        ntuple_omit.resolve(oid)


def test_ntuple_omit_happy(ntuple_omit):
    out = ntuple_omit.resolve("urn:nbn:de:gbv:089-332175294")
    assert out.endswith("089-332175294")


@pytest.mark.parametrize("oid", ["..", "a/b", "a\x00"])
def test_flat_direct_rejects(oid):
    with pytest.raises(ValueError):
        FlatDirectLayout().resolve(oid)


def test_flat_direct_happy():
    assert FlatDirectLayout().resolve("abc") == "abc"
