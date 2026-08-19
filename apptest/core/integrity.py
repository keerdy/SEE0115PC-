from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


UNVERIFIED = "UNVERIFIED"
INVALID_METADATA = "INVALID_METADATA"
MISMATCH = "MISMATCH"

_MD5_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class IntegrityMetadataError(ValueError):
    """Raised when an API/config integrity field is present but malformed."""


@dataclass(frozen=True)
class IntegrityExpectation:
    algorithm: str = ""
    expected_digest: str = ""
    expected_file_size: int | None = None
    status: str = UNVERIFIED
    source: str = "none"

    @property
    def expected_md5(self) -> str:
        return self.expected_digest if self.algorithm == "md5" else ""

    @property
    def expected_sha256(self) -> str:
        return self.expected_digest if self.algorithm == "sha256" else ""


def _first_value(metadata: dict[str, Any], keys: Iterable[str]) -> tuple[str, str]:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), f"metadata.{key}"
    return "", ""


def _parse_file_size(metadata: dict[str, Any]) -> int | None:
    if "file_size" not in metadata:
        return None
    value = metadata.get("file_size")
    if isinstance(value, bool):
        raise IntegrityMetadataError("file_size must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise IntegrityMetadataError("file_size must be a positive integer") from exc
    if parsed <= 0:
        raise IntegrityMetadataError("file_size must be a positive integer")
    return parsed


def resolve_integrity_expectation(
    metadata: dict[str, Any],
    *,
    configured_md5: str = "",
    configured_sha256: str = "",
    md5_keys: Iterable[str] = ("md5", "package_md5", "file_md5"),
    sha256_keys: Iterable[str] = ("sha256", "package_sha256", "file_sha256"),
) -> IntegrityExpectation:
    """Resolve metadata/configured integrity fields with strict format checks."""
    expected_file_size = _parse_file_size(metadata)

    metadata_sha256, sha256_source = _first_value(metadata, sha256_keys)
    configured_sha256 = configured_sha256.strip()
    if metadata_sha256 or configured_sha256:
        digest = metadata_sha256 or configured_sha256
        source = sha256_source or "config.sha256"
        if not _SHA256_PATTERN.fullmatch(digest):
            raise IntegrityMetadataError("sha256 must be exactly 64 hexadecimal characters")
        return IntegrityExpectation("sha256", digest.lower(), expected_file_size, "EXPECTED_SHA256", source)

    metadata_md5, md5_source = _first_value(metadata, md5_keys)
    configured_md5 = configured_md5.strip()
    if metadata_md5 or configured_md5:
        digest = metadata_md5 or configured_md5
        source = md5_source or "config.md5"
        if not _MD5_PATTERN.fullmatch(digest):
            raise IntegrityMetadataError("md5 must be exactly 32 hexadecimal characters")
        return IntegrityExpectation("md5", digest.lower(), expected_file_size, "EXPECTED_MD5", source)

    return IntegrityExpectation(expected_file_size=expected_file_size)


def validate_config_digest(value: str, algorithm: str, field_name: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    pattern = _SHA256_PATTERN if algorithm == "sha256" else _MD5_PATTERN
    if not pattern.fullmatch(value):
        length = 64 if algorithm == "sha256" else 32
        return f"{field_name} 必须是 {length} 位十六进制字符串"
    return None
