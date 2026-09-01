from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from memtomem_stm.release_check import (
    artifact_version,
    verify_artifacts,
    version_from_tag,
)


@pytest.mark.parametrize("tag", ["v0.2.0", "test-v0.2.0"])
def test_version_from_supported_tags(tag):
    assert version_from_tag(tag) == "0.2.0"


@pytest.mark.parametrize(
    "tag",
    ["0.2.0", "v", "release-v0.2.0", "v0.2.0/extra", "v0.2.0foo", "v0.2.0..1"],
)
def test_version_from_tag_rejects_ambiguous_shapes(tag):
    with pytest.raises(ValueError):
        version_from_tag(tag)


def _wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"memtomem_stm-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.3\nName: memtomem-stm\nVersion: {version}\n",
        )


def _sdist(path: Path, version: str) -> None:
    raw = f"Metadata-Version: 2.3\nName: memtomem-stm\nVersion: {version}\n".encode()
    info = tarfile.TarInfo(f"memtomem_stm-{version}/PKG-INFO")
    info.size = len(raw)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(raw))


def test_artifact_metadata_versions_are_read_from_both_formats(tmp_path):
    wheel = tmp_path / "memtomem_stm-0.2.0-py3-none-any.whl"
    sdist = tmp_path / "memtomem_stm-0.2.0.tar.gz"
    _wheel(wheel, "0.2.0")
    _sdist(sdist, "0.2.0")
    assert artifact_version(wheel) == "0.2.0"
    assert artifact_version(sdist) == "0.2.0"
    verify_artifacts(tmp_path, "0.2.0")


def test_artifact_version_mismatch_fails(tmp_path):
    _wheel(tmp_path / "memtomem_stm-0.2.0-py3-none-any.whl", "0.2.0")
    _sdist(tmp_path / "memtomem_stm-0.2.1.tar.gz", "0.2.1")
    with pytest.raises(ValueError, match="artifact version mismatch"):
        verify_artifacts(tmp_path, "0.2.0")


def test_two_wheels_do_not_substitute_for_the_sdist(tmp_path):
    _wheel(tmp_path / "memtomem_stm-0.2.0-py3-none-any.whl", "0.2.0")
    _wheel(tmp_path / "another-0.2.0-py3-none-any.whl", "0.2.0")
    with pytest.raises(ValueError, match="exactly one wheel and one source"):
        verify_artifacts(tmp_path, "0.2.0")
