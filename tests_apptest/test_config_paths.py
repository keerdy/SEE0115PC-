from __future__ import annotations

from pathlib import Path

from apptest.core.config import load_config


CONFIG_TEXT = """
device:
  host: 192.168.1.2
  port: 8080
cloud:
  app_package_current_url: https://example.test/app
run:
  report_output_root: artifacts
  download_dir: downloads
mobile: {}
"""


def test_load_config_uses_yaml_parent_by_default(tmp_path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_file = config_dir / "target.yaml"
    config_file.write_text(CONFIG_TEXT, encoding="utf-8")

    config = load_config(config_file)
    assert Path(config.run.report_output_root) == (config_dir / "artifacts").resolve()


def test_load_config_accepts_explicit_base_directory(tmp_path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_file = config_dir / "target.yaml"
    config_file.write_text(CONFIG_TEXT, encoding="utf-8")

    config = load_config(config_file, base_dir=tmp_path)
    assert Path(config.run.report_output_root) == (tmp_path / "artifacts").resolve()
