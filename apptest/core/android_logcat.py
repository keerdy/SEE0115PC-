from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


@dataclass
class AndroidLogcatSession:
    serial: str
    package_name: str
    output_file: Path
    pid: str


class AndroidAppLogcatCapture:
    def __init__(
        self,
        *,
        serial: str,
        package_name: str,
        output_file: str | Path,
        adb_path: str = "adb",
        clear_before_start: bool = True,
    ) -> None:
        self.serial = serial.strip()
        self.package_name = package_name.strip()
        self.output_file = Path(output_file)
        self.adb_path = adb_path
        self.clear_before_start = clear_before_start
        self._process: subprocess.Popen[str] | None = None
        self._handle = None
        self._session: AndroidLogcatSession | None = None

    @property
    def session(self) -> AndroidLogcatSession | None:
        return self._session

    def start(self) -> AndroidLogcatSession:
        if self._process is not None:
            raise RuntimeError("logcat capture already started")
        if not self.serial:
            raise ValueError("android serial is required")
        if not self.package_name:
            raise ValueError("android package name is required")

        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        if self.clear_before_start:
            self._run_adb("logcat", "-c", check=False)

        pid = self._resolve_pid()
        if not pid:
            raise RuntimeError(f"failed to resolve pid for package: {self.package_name}")

        self._handle = self.output_file.open("w", encoding="utf-8")
        self._handle.write(f"# package={self.package_name}\n")
        self._handle.write(f"# serial={self.serial}\n")
        self._handle.write(f"# pid={pid}\n")
        self._handle.write(f"# started_at={datetime.now().isoformat()}\n\n")
        self._handle.flush()

        try:
            cmd = self._build_adb_command("logcat", "--pid", pid, "-v", "threadtime")
            self._process = subprocess.Popen(
                cmd,
                stdout=self._handle,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            self._handle.close()
            self._handle = None
            raise
        self._session = AndroidLogcatSession(
            serial=self.serial,
            package_name=self.package_name,
            output_file=self.output_file,
            pid=pid,
        )
        return self._session

    def stop(self) -> Path:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._process = None

        if self._handle is not None:
            self._handle.write(f"\n# stopped_at={datetime.now().isoformat()}\n")
            self._handle.flush()
            self._handle.close()
            self._handle = None

        return self.output_file

    def _resolve_pid(self) -> str:
        result = self._run_adb("shell", "pidof", "-s", self.package_name, check=False)
        return result.stdout.strip()

    def _run_adb(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._build_adb_command(*args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            check=check,
        )

    def _build_adb_command(self, *args: str) -> list[str]:
        cmd = [self.adb_path]
        if self.serial:
            cmd.extend(["-s", self.serial])
        cmd.extend(args)
        return cmd
