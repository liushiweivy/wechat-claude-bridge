"""Config 校验测试：未知键拒绝、权限模式白名单、目录约束。"""
import json
from pathlib import Path

import pytest

from bridge import config as config_mod
from bridge.config import Config, load_config


def test_defaults_are_valid(tmp_path: Path):
    cfg = Config(workspace=str(tmp_path), allowed_dirs=(str(tmp_path),))
    cfg.validate()  # 不抛异常即可


def test_from_dict_rejects_unknown_key(tmp_path: Path):
    with pytest.raises(ValueError, match="未知配置项"):
        Config.from_dict({"workspace": str(tmp_path), "evil": 1})


def test_permission_mode_whitelist(tmp_path: Path):
    Config(workspace=str(tmp_path), allowed_dirs=(str(tmp_path),),
           default_mode="plan").validate()
    with pytest.raises(ValueError, match="非法 default_mode"):
        Config(workspace=str(tmp_path), allowed_dirs=(str(tmp_path),),
               default_mode="yolo").validate()


def test_workspace_must_be_inside_allowed_dirs(tmp_path: Path):
    other = tmp_path / "outside"
    ws = tmp_path / "ws"
    other.mkdir()
    ws.mkdir()
    with pytest.raises(ValueError, match="allowed_dirs"):
        Config(workspace=str(ws), allowed_dirs=(str(other),)).validate()


def test_poll_seconds_floor_and_msg_chars_range(tmp_path: Path):
    with pytest.raises(ValueError, match="poll_seconds"):
        Config(workspace=str(tmp_path), allowed_dirs=(str(tmp_path),),
               poll_seconds=0.1).validate()
    with pytest.raises(ValueError, match="max_msg_chars"):
        Config(workspace=str(tmp_path), allowed_dirs=(str(tmp_path),),
               max_msg_chars=10).validate()


def test_missing_allowed_dir_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="allowed_dirs 目录不存在"):
        Config(workspace=str(tmp_path),
               allowed_dirs=(str(tmp_path / "nope"),)).validate()


def test_load_config_missing_file_points_to_example(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")
    with pytest.raises(FileNotFoundError, match="config.example.json"):
        load_config()


def test_load_config_roundtrip(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "workspace": str(tmp_path),
        "allowed_dirs": [str(tmp_path)],
        "poll_seconds": 3,
    }), encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    cfg = load_config()
    assert cfg.poll_seconds == 3
