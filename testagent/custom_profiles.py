"""PC-local reusable schemes for ``custom_test/C01``.

Only the active configuration is sent to a device.  Schemes are intentionally
stored on the PC, so one device's C01 persistence does not overwrite another
operator's reusable plans.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import logging
import os
from pathlib import Path
import shutil
import time
import uuid
from typing import Any, Iterable, Mapping

from .custom_config import CustomConfig, CustomConfigError, CustomStep

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None


PROFILE_FILE_VERSION = 1
PROFILE_LOCK_TIMEOUT_SECONDS = 5.0
PROFILE_LOCK_RETRY_SECONDS = 0.05

_LOGGER = logging.getLogger(__name__)


class CustomProfileStoreError(OSError):
    """A scheme file cannot be safely read or replaced."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class CustomProfile:
    profile_id: str
    name: str
    created_at: str
    updated_at: str
    config: CustomConfig

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], _capabilities: object | None = None,
    ) -> "CustomProfile":
        profile_id = payload.get("id")
        name = payload.get("name")
        created_at = payload.get("created_at")
        updated_at = payload.get("updated_at")
        config_payload = payload.get("config")
        if (not isinstance(profile_id, str) or not profile_id or
                not isinstance(name, str) or not name.strip() or
                not isinstance(created_at, str) or not isinstance(updated_at, str) or
                not isinstance(config_payload, Mapping)):
            raise CustomConfigError("本地方案文件包含无效内容")
        raw_steps = config_payload.get("steps")
        if not isinstance(raw_steps, list):
            raise CustomConfigError("本地方案步骤无效")
        steps: list[CustomStep] = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, Mapping):
                raise CustomConfigError("本地方案步骤无效")
            values = {
                key: raw_step.get(key, 0)
                for key in (
                    "action", "page", "arg0", "arg1", "arg2", "page_wait_mode",
                    "step_interval_ms", "run_once", "video_canvas",
                    "check_ui_complete", "check_ui_frozen",
                )
            }
            params = raw_step.get("params", [0, 0, 0, 0])
            if (not all(isinstance(value, int) and not isinstance(value, bool) for value in values.values()) or
                    not isinstance(params, list) or len(params) != 4 or
                    not all(isinstance(value, int) and not isinstance(value, bool) for value in params)):
                raise CustomConfigError("本地方案步骤参数无效")
            steps.append(CustomStep(**values, params=tuple(params)))
        fields = (
            "cycles", "page_settle_ms", "step_interval_ms", "media_interval_ms",
            "cycle_interval_ms", "photo_check_mode", "photo_check_every_cycles",
            "video_check_mode", "video_check_every_cycles", "photo_cleanup_every_cycles",
            "video_cleanup_every_cycles", "cleanup_before_wait_index",
            "cleanup_between_wait_index", "cleanup_after_wait_index",
        )
        values = {field: config_payload.get(field, 0) for field in fields}
        if not all(isinstance(value, int) for value in values.values()):
            raise CustomConfigError("本地方案参数无效")
        # A PC scheme may have been saved for a different firmware.  Keep it
        # visible and reject it only when the operator tries to load it on an
        # incompatible device, rather than silently deleting it on the next save.
        config = CustomConfig(steps=steps, **values)
        return cls(
            profile_id=profile_id,
            name=name.strip(),
            created_at=created_at,
            updated_at=updated_at,
            config=config,
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.profile_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "config": self.config.as_payload(),
        }


class CustomProfileStore:
    """Small atomic JSON store with cross-process locking and a last-good backup."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._load_error: str | None = None
        self._load_warning: str | None = None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    @property
    def load_warning(self) -> str | None:
        return self._load_warning

    def load(self, capabilities: object | None = None) -> list[CustomProfile]:
        self._load_error = None
        self._load_warning = None
        try:
            with self._file_lock():
                if not self._path.exists():
                    return []
                return self._read_profiles(self._path, capabilities)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, CustomConfigError) as exc:
            self._load_error = str(exc) or exc.__class__.__name__
            return []

    def save(self, profiles: Iterable[CustomProfile]) -> None:
        if self._load_error is not None or self._load_warning is not None:
            raise CustomProfileStoreError(
                "方案文件无法安全覆盖，已禁止覆盖；请先备份或修复 custom_test_profiles.json"
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock():
            document = {
                "version": PROFILE_FILE_VERSION,
                "profiles": [profile.as_payload() for profile in profiles],
            }
            temporary = self._path.with_name(
                self._path.name + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                with temporary.open("w", encoding="utf-8") as fp:
                    fp.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
                    fp.flush()
                    os.fsync(fp.fileno())
                self._backup_current_file()
                temporary.replace(self._path)
                self._fsync_parent(self._path)
            finally:
                if temporary.exists():
                    temporary.unlink()

    @property
    def backup_path(self) -> Path:
        return self._path.with_name(self._path.name + ".bak")

    @property
    def lock_path(self) -> Path:
        return self._path.with_name(self._path.name + ".lock")

    def _read_profiles(
        self, path: Path, capabilities: object | None = None,
    ) -> list[CustomProfile]:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping) or document.get("version") != PROFILE_FILE_VERSION:
            raise ValueError("unsupported profile file")
        raw_profiles = document.get("profiles", [])
        if not isinstance(raw_profiles, list):
            raise ValueError("profiles is not a list")
        profiles: list[CustomProfile] = []
        invalid_items: list[int] = []
        for index, item in enumerate(raw_profiles, start=1):
            try:
                profiles.append(CustomProfile.from_payload(item, capabilities))
            except (AttributeError, TypeError, CustomConfigError):
                invalid_items.append(index)
        if invalid_items:
            self._load_warning = (
                "方案文件包含无效条目：第 "
                + ", ".join(str(index) for index in invalid_items)
                + " 项已忽略"
            )
        return profiles

    @contextmanager
    def _file_lock(self):
        """Serialize profile reads/writes across GUI processes on both OS families."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            locked = False
            deadline = time.monotonic() + PROFILE_LOCK_TIMEOUT_SECONDS
            while not locked:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    elif msvcrt is not None:
                        lock_file.seek(0, os.SEEK_END)
                        if lock_file.tell() == 0:
                            lock_file.write(b"\0")
                            lock_file.flush()
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - unsupported Python platform
                        _LOGGER.warning("profile_file_lock_unavailable path=%s", self.lock_path)
                    locked = True
                except OSError as exc:
                    busy = isinstance(exc, BlockingIOError) or exc.errno in (errno.EACCES, errno.EAGAIN)
                    if not busy or time.monotonic() >= deadline:
                        raise CustomProfileStoreError(
                            f"方案文件被其他 TestAgent 实例占用：{self._path}"
                        ) from exc
                    time.sleep(PROFILE_LOCK_RETRY_SECONDS)
            try:
                yield
            finally:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    _LOGGER.warning("profile_file_unlock_failed path=%s", self.lock_path)

    def _backup_current_file(self) -> None:
        """Publish a durable backup before replacing the active profile file."""
        if not self._path.exists():
            return
        temporary_backup = self.backup_path.with_name(
            self.backup_path.name + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            with self._path.open("rb") as source, temporary_backup.open("wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            temporary_backup.replace(self.backup_path)
            self._fsync_parent(self.backup_path)
        finally:
            if temporary_backup.exists():
                temporary_backup.unlink()

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            _LOGGER.warning(
                "directory fsync unavailable for %s, skipping (file itself is already fsynced): %s",
                path.parent,
                exc,
            )

    @staticmethod
    def new(name: str, config: CustomConfig) -> CustomProfile:
        now = _now()
        return CustomProfile(uuid.uuid4().hex, name.strip(), now, now, config)

    @staticmethod
    def replace(existing: CustomProfile, name: str, config: CustomConfig) -> CustomProfile:
        return CustomProfile(existing.profile_id, name.strip(), existing.created_at, _now(), config)
