"""CubeMX 数据库：引脚能力（`mcu`）与外设模式（`modes`）。

这一层是纯只读的权威事实来源，不依赖 `.ioc`，也不依赖 CubeMX 进程。
`cubemx_mcp.ioc` 负责"文件长什么样"，这里负责"什么配置才算对"。
"""

from __future__ import annotations

from pathlib import Path

from ..ioc.signals import gpio_mode_of
from .dma import DmaDb, DmaRequest
from .irq import Irq, IrqDb
from .mcu import DbError, IpRef, McuDb, PinCap
from .modes import ModeDb, ModeDef, ParamDef

__all__ = [
    "DbError",
    "Device",
    "DmaDb",
    "DmaRequest",
    "IpRef",
    "Irq",
    "IrqDb",
    "McuDb",
    "ModeDb",
    "ModeDef",
    "ParamDef",
    "PinCap",
    "gpio_mode_of",
]

#: ``NVIC``/``DMA`` 在 MCU 的 XML 里也是普通 ``<IP InstanceName=...>``，所以
#: `McuDb.modes_path` 直接就能解析出它们各自的 Modes 文件 —— 不需要另外拼路径。
NVIC_IP = "NVIC"
DMA_IP = "DMA"


class Device:
    """一颗 MCU 的全部元数据，模式库按需载入并缓存。"""

    def __init__(self, mcu: McuDb) -> None:
        self.mcu = mcu
        self._loaded: dict[str, object | None] = {}

    @classmethod
    def open(cls, mcu_name: str, root: Path | None = None) -> Device:
        return cls(McuDb.load(mcu_name, root))

    def _load_one(self, key: str, instance: str, loader):
        """按实例名缓存地载入一份库；文件缺失或格式不认识都返回 None。

        吞掉 `DbError` 是刻意的：数据库格式变了（或这颗 MCU 没有那份表）的正确
        反应是"这一页看不了"，而不是整个命令崩掉。调用方拿到 None 时应该**明说
        看不了**，别当成"空"。
        """
        if key in self._loaded:
            return self._loaded[key]
        path = self.mcu.modes_path(instance)
        db = None
        if path is not None:
            try:
                db = loader(path)
            except DbError:
                db = None
        self._loaded[key] = db
        return db

    def modes(self, instance: str) -> ModeDb | None:
        """某个外设实例的模式库；数据库里没有该文件时返回 None。"""
        return self._load_one(f"modes:{instance}", instance, ModeDb.load)

    def irqs(self) -> IrqDb | None:
        """这颗 MCU 的中断表（``NVIC-<MCU>_Modes.xml``）；没有则 None。

        注意它**不能用 `modes()` 拿** —— 那份文件一个 ``<RefMode>`` 都没有，
        `ModeDb` 会按"格式可能变了"直接抛错。见 `cubemx_mcp.db.irq`。
        """
        return self._load_one("nvic", NVIC_IP, IrqDb.load)

    def dma(self) -> DmaDb | None:
        """这颗 MCU 的 DMA 请求表（``DMA-*_Modes.xml``）；没有则 None。"""
        return self._load_one("dma", DMA_IP, DmaDb.load)

    def modes_for_signal(self, signal: str) -> tuple[str, ModeDb] | None:
        """从一个 signal 名一路找到它所属外设的模式库。"""
        instance = self.mcu.signal_owner(signal)
        if instance is None:
            return None
        db = self.modes(instance)
        return (instance, db) if db is not None else None

    def __repr__(self) -> str:
        n = sum(1 for k in self._loaded if k.startswith("modes:"))
        return f"Device({self.mcu.name!r}, {n} libs loaded)"
