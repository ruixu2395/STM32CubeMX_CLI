"""`cubemx_mcp.settings` + `config.resolve_db_root` —— 数据库路径从哪来。

打包成 exe 之后，"CubeMX 装在哪"不能再靠猜常见路径。这个文件测的是那条三级解析
（环境变量 → 用户配置文件 → 自动探测）以及它**怎么解释自己的结论**。

`conftest.py` 里的 autouse fixture 已经把 `CUBEMX_CLI_CONFIG` 指到 tmp，所以这里可以
放心读写配置，不会碰到开发机上的真实配置。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cubemx_mcp import config, settings


def _clear_db_env(monkeypatch) -> None:
    """把两个环境变量清干净，让三级解析的起点可控。"""
    monkeypatch.delenv("ST_CUBEMX_DB", raising=False)
    monkeypatch.delenv("ST_CUBEMX_EXE", raising=False)


@pytest.fixture
def install_dir(tmp_path: Path) -> Path:
    """假的 CubeMX 安装目录，旁边有 db/mcu 和一堆**来骗人的邻居**。"""
    base = tmp_path / "cube"
    (base / "db" / "mcu" / "IP").mkdir(parents=True)
    (base / "db" / "mcu" / "STM32F407Z(E-G)Tx.xml").write_text("<Mcu/>", encoding="utf-8")
    (base / "db" / "mcu" / "IP" / "USART-1_0_Modes.xml").write_text("<M/>", encoding="utf-8")
    (base / "db" / "mcu" / "families.xml").write_text("<F/>", encoding="utf-8")
    # db 顶层那个 package.xml 就是让"含任意 .xml"这种朴素判据失手的东西
    (base / "db" / "package.xml").write_text("<P/>", encoding="utf-8")
    return base


@pytest.fixture
def exe(install_dir: Path) -> Path:
    p = install_dir / "STM32CubeMX.exe"
    p.write_bytes(b"MZ")
    return p


# ============================================================== 认目录


def test_db_root_is_recognised(install_dir: Path):
    assert settings.is_db_root(install_dir / "db" / "mcu")


def test_the_db_dir_itself_is_not_a_db_root(install_dir: Path):
    """`db` 顶层只有一个 package.xml —— 少了 mcu 那一层是最常见的错。

    朴素的"含任意 .xml"判据会在这里放过去，所以判据必须是 STM32*.xml。
    """
    assert not settings.is_db_root(install_dir / "db")


def test_the_ip_dir_is_not_a_db_root(install_dir: Path):
    """`db/mcu/IP` 里的 xml 全是 `*_Modes.xml`，不是 MCU 引脚文件。"""
    assert not settings.is_db_root(install_dir / "db" / "mcu" / "IP")


def test_a_missing_dir_is_not_a_db_root(tmp_path: Path):
    assert not settings.is_db_root(tmp_path / "nope")


def test_a_file_is_not_a_db_root(tmp_path: Path):
    f = tmp_path / "x.xml"
    f.write_text("<x/>", encoding="utf-8")
    assert not settings.is_db_root(f)


def test_find_db_root_accepts_the_install_dir(install_dir: Path):
    """让用户指安装目录就行 —— 要求他知道 `db\\mcu` 那一层不友好。"""
    assert settings.find_db_root(install_dir) == install_dir / "db" / "mcu"


def test_find_db_root_accepts_db(install_dir: Path):
    assert settings.find_db_root(install_dir / "db") == install_dir / "db" / "mcu"


def test_find_db_root_accepts_db_mcu(install_dir: Path):
    db = install_dir / "db" / "mcu"
    assert settings.find_db_root(db) == db


def test_find_db_root_gives_up_on_an_unrelated_dir(tmp_path: Path):
    junk = tmp_path / "junk"
    junk.mkdir()
    assert settings.find_db_root(junk) is None


# ============================================================== 读写配置文件


def test_a_missing_config_file_is_not_an_error(tmp_path: Path):
    """文件不存在就是"还没配过"，不是错误 —— 第一次运行必然是这种状态。"""
    cfg = settings.load(tmp_path / "nope.json")
    assert cfg.db_root is None and cfg.problems == []


def test_save_then_load_roundtrip(tmp_path: Path, install_dir: Path):
    path = tmp_path / "cfg" / "config.json"
    db = install_dir / "db" / "mcu"

    settings.save(settings.Settings(db_root=db, cube_mx_exe=install_dir / "STM32CubeMX.exe"), path)

    cfg = settings.load(path)
    assert cfg.db_root == db
    assert cfg.cube_mx_exe == install_dir / "STM32CubeMX.exe"
    assert cfg.problems == []


def test_the_file_is_readable_json_with_a_version(tmp_path: Path, install_dir: Path):
    """写出来的得是标准 JSON，且带版本号 —— 那是将来做迁移的唯一抓手。"""
    path = tmp_path / "config.json"
    settings.save(settings.Settings(db_root=install_dir / "db" / "mcu"), path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == settings.VERSION
    assert raw["db_root"] == str(install_dir / "db" / "mcu")
    assert "cube_mx_exe" not in raw  # 没给就不写空键


def test_save_leaves_no_temp_files(tmp_path: Path, install_dir: Path):
    path = tmp_path / "config.json"
    settings.save(settings.Settings(db_root=install_dir / "db" / "mcu"), path)
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_corrupt_config_degrades_instead_of_crashing(tmp_path: Path):
    """手改坏一个 JSON 不该让整个工具用不了 —— 退化成"没配过"并说明原因。"""
    path = tmp_path / "config.json"
    path.write_text("{这不是 JSON", encoding="utf-8")

    cfg = settings.load(path)
    assert cfg.db_root is None
    assert cfg.problems and "读不了" in cfg.problems[0]


def test_a_config_that_is_not_an_object(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert settings.load(path).problems


def test_a_future_version_warns_but_still_reads(tmp_path: Path, install_dir: Path):
    """版本对不上时说一句，但仍然按已知字段尽力读 —— 别装作没看见，也别整个丢掉。"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 99, "db_root": str(install_dir / "db" / "mcu")}),
        encoding="utf-8",
    )
    cfg = settings.load(path)
    assert cfg.db_root == install_dir / "db" / "mcu"
    assert any("版本" in p for p in cfg.problems)


def test_a_bad_field_type_is_reported(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "db_root": 123}), encoding="utf-8")
    cfg = settings.load(path)
    assert cfg.db_root is None
    assert any("db_root" in p for p in cfg.problems)


# ============================================================== 三级解析


def test_config_file_wins_over_auto_detection(monkeypatch, tmp_path: Path, install_dir: Path, exe: Path):
    """配过的路径比猜出来的可信 —— 自动探测永远排最后。"""
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: exe)

    other = tmp_path / "elsewhere" / "db" / "mcu"
    other.mkdir(parents=True)
    (other / "STM32F407Z(E-G)Tx.xml").write_text("<Mcu/>", encoding="utf-8")
    settings.save(settings.Settings(db_root=other))

    res = config.resolve_db_root()
    assert res.path == other
    assert res.source == "config"


def test_env_wins_over_everything(monkeypatch, tmp_path: Path, install_dir: Path, exe: Path):
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: exe)
    settings.save(settings.Settings(db_root=install_dir / "db" / "mcu"))

    env_db = tmp_path / "env" / "mcu"
    env_db.mkdir(parents=True)
    (env_db / "STM32F407Z(E-G)Tx.xml").write_text("<Mcu/>", encoding="utf-8")
    monkeypatch.setenv("ST_CUBEMX_DB", str(env_db))

    res = config.resolve_db_root()
    assert res.path == env_db
    assert res.source == "env"


def test_auto_detection_is_the_last_resort(monkeypatch, exe: Path):
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: exe)
    res = config.resolve_db_root()
    assert res.path == exe.parent / "db" / "mcu"
    assert res.source == "auto"


def test_nothing_found(monkeypatch, tmp_path: Path):
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: None)
    res = config.resolve_db_root()
    assert res.path is None
    assert res.source == "none"
    assert not res


# --------------------------------------------------------------------------
# 这一组是本次改动的重点：**设错了不该等于没设**。
# 早先的实现里，ST_CUBEMX_DB 指向一个不存在的目录就直接返回 None，
# 于是把变量拼错一次 = 把整个数据库功能关掉，而提示语还叫人去"设置 ST_CUBEMX_DB"。
# --------------------------------------------------------------------------


def test_a_broken_env_var_falls_through_instead_of_giving_up(
    monkeypatch, install_dir: Path, exe: Path
):
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: exe)
    settings.save(settings.Settings(db_root=install_dir / "db" / "mcu"))
    monkeypatch.setenv("ST_CUBEMX_DB", "E:/nope/db/mcu")

    res = config.resolve_db_root()
    # 没有瘫掉：接着用了配置文件
    assert res.path == install_dir / "db" / "mcu"
    assert res.source == "config"
    # 而且**说出来了**是环境变量的问题，不是笼统的"没找到数据库"
    assert any("ST_CUBEMX_DB" in p and "已忽略" in p for p in res.problems)


def test_a_broken_env_var_still_falls_through_to_auto(monkeypatch, exe: Path):
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: exe)
    monkeypatch.setenv("ST_CUBEMX_DB", "E:/nope/db/mcu")

    res = config.resolve_db_root()
    assert res.source == "auto"
    assert res.problems


def test_an_env_var_pointing_at_the_wrong_layer_is_accepted_but_flagged(
    monkeypatch, install_dir: Path
):
    """用户显式给的路径就认（可能是我们没见过布局），但要说清它看着少了一层。"""
    _clear_db_env(monkeypatch)
    db = install_dir / "db"
    monkeypatch.setenv("ST_CUBEMX_DB", str(db))

    res = config.resolve_db_root()
    assert res.path == db
    assert res.source == "env"
    assert any("db\\mcu" in p for p in res.problems)


def test_a_stale_config_path_falls_through_to_auto(monkeypatch, tmp_path: Path, exe: Path):
    """CubeMX 挪了位置 → 配置文件过期 → 报出来再降级，不静默换个源。"""
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: exe)
    settings.save(settings.Settings(db_root=tmp_path / "moved" / "db" / "mcu"))

    res = config.resolve_db_root()
    assert res.source == "auto"
    assert any("不存在" in p for p in res.problems)


def test_a_bad_env_exe_is_reported(monkeypatch):
    """把 ST_CUBEMX_EXE 拼错时，提示必须点名这个变量 —— 以前它会让人去设
    ST_CUBEMX_DB，于是怎么设都没用。"""
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("ST_CUBEMX_EXE", "E:/nope/STM32CubeMX.exe")

    res = config.resolve_db_root()
    assert res.path is None
    assert any("ST_CUBEMX_EXE" in p for p in res.problems)


def test_corrupt_config_problems_surface_through_resolution(monkeypatch, exe: Path):
    """配置文件坏了也要一路传到调用方，而不是悄悄吞掉。"""
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: exe)
    settings.config_path().write_text("{坏的", encoding="utf-8")

    res = config.resolve_db_root()
    assert res.path == exe.parent / "db" / "mcu"  # 照样能用
    assert any("读不了" in p for p in res.problems)  # 但说了


# ============================================================== 兼容性


def test_db_root_is_still_a_plain_path(monkeypatch, exe: Path):
    """`db_root()` 是薄包装 —— 四个既有调用点都靠这个签名。"""
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: exe)
    assert config.db_root() == exe.parent / "db" / "mcu"


def test_db_root_returns_none_when_there_is_nothing(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(config, "cube_mx_exe", lambda: None)
    assert config.db_root() is None
