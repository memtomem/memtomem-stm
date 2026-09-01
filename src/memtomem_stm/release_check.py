"""Fail-closed release tag and distribution metadata verification."""

from __future__ import annotations

import argparse
import email.parser
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path

from memtomem_stm import __version__

_VERSION_PATTERN = (
    r"[0-9]+(?:\.[0-9]+)*"
    r"(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?"
    r"(?:\.dev[0-9]+)?"
    r"(?:\+[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*)?"
)
_TAG_RE = re.compile(rf"^(?:test-)?v(?P<version>{_VERSION_PATTERN})$")


def version_from_tag(tag: str) -> str:
    match = _TAG_RE.fullmatch(tag)
    if match is None:
        raise ValueError(f"release tag must be vVERSION or test-vVERSION, got {tag!r}")
    return match.group("version")


def project_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml is missing project.version")
    return version


def verify_source_versions(root: Path, tag: str) -> str:
    tag_version = version_from_tag(tag)
    configured = project_version(root)
    if tag_version != configured or configured != __version__:
        raise ValueError(
            "release version mismatch: "
            f"tag={tag_version}, pyproject={configured}, runtime={__version__}"
        )
    return tag_version


def _metadata_version(raw: bytes, artifact: Path) -> str:
    message = email.parser.BytesParser().parsebytes(raw)
    versions = message.get_all("Version", [])
    if len(versions) != 1 or not versions[0]:
        raise ValueError(f"{artifact.name} metadata must have exactly one Version field")
    return versions[0]


def artifact_version(artifact: Path) -> str:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError(f"{artifact.name} must contain exactly one METADATA file")
            return _metadata_version(archive.read(names[0]), artifact)
    if artifact.name.endswith(".tar.gz"):
        with tarfile.open(artifact, "r:gz") as archive:
            members = [
                member for member in archive.getmembers() if member.name.endswith("/PKG-INFO")
            ]
            if len(members) != 1:
                raise ValueError(f"{artifact.name} must contain exactly one PKG-INFO file")
            handle = archive.extractfile(members[0])
            if handle is None:
                raise ValueError(f"cannot read PKG-INFO from {artifact.name}")
            return _metadata_version(handle.read(), artifact)
    raise ValueError(f"unsupported release artifact: {artifact.name}")


def verify_artifacts(dist: Path, expected_version: str) -> None:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("dist must contain exactly one wheel and one source distribution")
    for artifact in (*wheels, *sdists):
        actual = artifact_version(artifact)
        if actual != expected_version:
            raise ValueError(
                f"artifact version mismatch for {artifact.name}: "
                f"expected={expected_version}, actual={actual}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()

    expected = verify_source_versions(args.root, args.tag)
    if args.dist is not None:
        verify_artifacts(args.dist, expected)
    print(f"release metadata verified: {expected}")


if __name__ == "__main__":
    main()
