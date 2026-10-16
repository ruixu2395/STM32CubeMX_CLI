"""测试夹具。

样本工程是 `tests/data/Usb_uart.ioc`（真实 CubeMX 产物：STM32F407ZGT6，
LQFP144，USART1 + DMA）。

**需要数据库的测试会自动跳过**：CubeMX 的 ``db/mcu`` 不是每个环境都有，
用 ``ST_CUBEMX_DB`` 指过去即可。跳过而不是失败，是因为"没有数据库"和
"代码写错了"是两回事 —— 但跳过会明说原因，不静默通过。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cubemx_mcp import settings
from cubemx_mcp.config import db_root
from cubemx_mcp.db import Device
from cubemx_mcp.ioc.project import IocProject
from cubemx_mcp.ioc.raw import IocDocument

DATA = Path(__file__).parent / "data"
SAMPLE = DATA / "Usb_uart.ioc"

#: 样本工程用的型号。**它就是数据库文件名**，不是猜的。
SAMPLE_MCU = "STM32F407Z(E-G)Tx"


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path: Path, monkeypatch) -> None:
    """把用户配置文件指到 tmp 下一个**不存在**的路径。

    这条是必须的，不是洁癖。`test_cli.py` 里大量断言依赖数据库
    （`pin.unsupported_signal` / `mode.missing_signals` / `virtual.missing`，
    还有"工程是干净的"），但它**从不声明 `device` fixture** —— 它吃的是当前环境
    解析出来的数据库。一旦开发机上真存在 ``%APPDATA%\\cubemx-cli\\config.json``，
    这些用例的解析结果就会跟着变，静默换一套行为、而且是那种"在我机器上过"的漂移。

    指到 tmp 之后，配置层对现有用例是**彻底惰性**的；要测配置层的用例自己覆盖它。
    """
    monkeypatch.setenv(settings.CONFIG_ENV, str(tmp_path / "no-such-config.json"))


@pytest.fixture
def sample_path() -> Path:
    return SAMPLE


@pytest.fixture
def sample_ioc() -> Path:
    """只读地看待样本工程本身（字节级往返之类的测试用）。"""
    return SAMPLE


@pytest.fixture
def sample_doc() -> IocDocument:
    """样本的行级文档 —— 不经过 `IocProject` 的结构化视图。"""
    return IocDocument.load(SAMPLE)


@pytest.fixture
def tmp_ioc(tmp_path: Path, sample_path: Path) -> Path:
    """样本工程的临时副本 —— 测试改它，不动仓库里的原件。"""
    target = tmp_path / "Proj.ioc"
    shutil.copy(sample_path, target)
    return target


@pytest.fixture
def project(tmp_ioc: Path) -> IocProject:
    """没有数据库的工程（只读/写文件文本，不查引脚能力）。"""
    return IocProject.load(tmp_ioc)


@pytest.fixture(scope="session")
def device() -> Device:
    if db_root() is None:
        pytest.skip(
            "没有 CubeMX 数据库：把 ST_CUBEMX_DB 指向 <STM32CubeMX>\\db\\mcu"
        )
    try:
        return Device.open(SAMPLE_MCU)
    except Exception as exc:  # noqa: BLE001 - 数据库布局变化时给出可读原因
        pytest.skip(f"数据库里没有 {SAMPLE_MCU}：{exc}")


@pytest.fixture
def db_project(tmp_ioc: Path, device: Device) -> IocProject:
    """挂了数据库的工程 —— 引脚能力、模式、参数校验才可用。"""
    return IocProject.load(tmp_ioc, device=device)
