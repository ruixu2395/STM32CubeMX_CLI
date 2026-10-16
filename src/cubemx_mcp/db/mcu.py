"""权威引脚能力库：``db/mcu/<Mcu.Name>.xml``。

这是整个工具"配置准不准"的地基。文件名的 **就是** ``.ioc`` 里 ``Mcu.Name``
的字面值（``STM32F407Z(E-G)Tx``），所以定位不需要任何猜测。

每个 ``<Pin>`` 元素列出这颗 MCU 在该封装下这个脚**实际能承载的全部 signal**：

.. code-block:: xml

    <Pin Name="PA11" Position="103" Type="I/O">
        <Signal Name="ADC1_EXTI11"/>
        <Signal Name="CAN1_RX"/>
        <Signal Name="TIM1_CH4"/>
        <Signal Name="USART1_CTS"/>
        <Signal Name="USB_OTG_FS_DM"/>
        <Signal IOModes="Input,Output,Analog,EVENTOUT,EXTI" Name="GPIO"/>
    </Pin>

注意 ``PA11`` 有 ``USART1_CTS`` 但**没有** ``USART1_RX`` —— 光靠 signal 名的
前缀启发式完全看不出这一点，只能查这张表。

同一份文件还给出该型号可用的外设实例及版本（``<IP InstanceName="USART1"
Name="USART" Version="sci2_v1_2_Cube"/>``），版本号决定去读哪个 ``*_Modes.xml``。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import config
from ..ioc.signals import gpio_exti_line, gpio_mode_of, is_gpio_signal, pin_number
from .xml import DbError, children, descendants, parse


@dataclass(frozen=True)
class PinCap:
    """一个引脚在这颗 MCU 上的能力。"""

    name: str
    position: int | None
    type: str | None
    #: 该脚能承载的全部 signal 名（不含 ``GPIO``）
    signals: frozenset[str]
    #: ``GPIO`` 那条 signal 的 ``IOModes``，如 Input/Output/Analog/EXTI
    gpio_modes: frozenset[str]
    #: 脚号（``PA0-WKUP`` -> ``0``），用于校验 EXTI 线号
    number: int | None = None

    def can(self, signal: str) -> bool:
        """这个脚能不能承载 ``signal``。

        EXTI 要单独判：``GPXTI5`` 这种字符串在任何 MCU 库里都查不到（CubeMX
        自己合成的），只能要求该脚 ``IOModes`` 含 ``EXTI`` **且**线号等于脚号。
        """
        line = gpio_exti_line(signal)
        if line is not None:
            return "EXTI" in self.gpio_modes and line == self.number
        if is_gpio_signal(signal):
            mode = gpio_mode_of(signal)
            return mode is not None and mode in self.gpio_modes
        return signal in self.signals


@dataclass(frozen=True)
class IpRef:
    """MCU 上一个外设实例。"""

    instance: str  # USART1
    name: str  # USART
    version: str  # sci2_v1_2_Cube

    @property
    def modes_file_name(self) -> str:
        """它对应的模式库文件名：``USART-sci2_v1_2_Cube_Modes.xml``。"""
        return f"{self.name}-{self.version}_Modes.xml"


class McuDb:
    """一颗 MCU 的引脚与外设全貌。"""

    def __init__(
        self,
        name: str,
        pins: dict[str, PinCap],
        ips: dict[str, IpRef],
        path: Path | None = None,
    ) -> None:
        self.name = name
        self.pins = pins
        self.ips = ips
        self.path = path
        #: 引脚按 Position 排序 —— 自动分配时用它保证结果稳定可复现
        self._ordered = tuple(
            sorted(pins.values(), key=lambda p: (p.position is None, p.position or 0))
        )

    # ---------------------------------------------------------------- 载入

    @classmethod
    def load(cls, mcu_name: str, root: Path | None = None) -> McuDb:
        """按 ``Mcu.Name`` 载入。数据库缺失或没有这颗型号时抛 `DbError`。"""
        base = root or config.db_root()
        if base is None:
            raise DbError(
                "找不到 CubeMX 数据库。设置环境变量 ST_CUBEMX_DB 指向 "
                r"<STM32CubeMX>\db\mcu，或把 CubeMX 装在默认位置。"
            )
        path = base / f"{mcu_name}.xml"
        if not path.exists():
            raise DbError(f"{base} 下没有 {mcu_name}.xml（Mcu.Name 对不上？）")
        return _load_cached(str(path), mcu_name)

    # ---------------------------------------------------------------- 查询

    def ordered_pins(self) -> tuple[PinCap, ...]:
        """按封装位置排序的全部引脚。"""
        return self._ordered

    def signals_of(self, pin: str) -> frozenset[str]:
        """该脚能承载的全部 signal（含 ``GPIO``）。"""
        cap = self.pins.get(pin)
        return cap.signals if cap else frozenset()

    def can_pin_do(self, pin: str, signal: str) -> bool:
        cap = self.pins.get(pin)
        return cap.can(signal) if cap else False

    def pins_for_signal(self, signal: str) -> tuple[str, ...]:
        """哪些脚能承载这个完整 signal 名。"""
        return tuple(p.name for p in self._ordered if p.can(signal))

    def pins_for(self, instance: str, short: str) -> tuple[str, ...]:
        """``instance`` 的 ``short`` 信号能落在哪些脚上。

        例如 ``pins_for("USART2", "TX")`` -> ``("PA2", "PD5")``。
        匹配用的是「``<instance>_`` 前缀 + 尾段完全相等」，所以 ``USART1``
        不会误吞 ``USART10``。
        """
        prefix = instance + "_"
        out = []
        for cap in self._ordered:
            for sig in cap.signals:
                if sig.startswith(prefix) and sig[len(prefix):] == short:
                    out.append(cap.name)
                    break
        return tuple(out)

    def ip(self, instance: str) -> IpRef | None:
        return self.ips.get(instance)

    def has_ip(self, instance: str) -> bool:
        return instance in self.ips

    def signal_owner(self, signal: str) -> str | None:
        """从 signal 名反查外设实例，用**权威实例名表**精确匹配。

        ``GPIO_Output`` / ``GPXTI0`` 返回 None（GPIO 不是可配置外设）。
        """
        if not signal or is_gpio_signal(signal):
            return None
        best: str | None = None
        for instance in self.ips:
            if (signal == instance or signal.startswith(instance + "_")
                    or signal.startswith(instance + "-")):
                if best is None or len(instance) > len(best):
                    best = instance
        return best

    def modes_path(self, instance: str) -> Path | None:
        """该实例的模式库文件路径（不存在则 None）。"""
        ref = self.ips.get(instance)
        if ref is None or self.path is None:
            return None
        p = self.path.parent / "IP" / ref.modes_file_name
        return p if p.exists() else None

    def __repr__(self) -> str:
        return f"McuDb({self.name!r}, {len(self.pins)} pins, {len(self.ips)} ips)"


@lru_cache(maxsize=8)
def _load_cached(path: str, name: str) -> McuDb:
    root = parse(path)
    pins: dict[str, PinCap] = {}
    for el in descendants(root, "Pin"):
        pin_name = el.get("Name")
        if not pin_name:
            continue
        signals: set[str] = set()
        gpio_modes: set[str] = set()
        for sig in children(el, "Signal"):
            sname = sig.get("Name")
            if not sname:
                continue
            if sname == "GPIO":
                raw = sig.get("IOModes") or ""
                gpio_modes.update(x.strip() for x in raw.split(",") if x.strip())
            else:
                signals.add(sname)
        pos = el.get("Position")
        try:
            number = pin_number(pin_name)
        except ValueError:
            number = None
        pins[pin_name] = PinCap(
            name=pin_name,
            position=int(pos) if pos and pos.isdigit() else None,
            type=el.get("Type"),
            signals=frozenset(signals),
            gpio_modes=frozenset(gpio_modes),
            number=number,
        )

    ips: dict[str, IpRef] = {}
    for el in descendants(root, "IP"):
        inst = el.get("InstanceName")
        if not inst:
            continue
        ips[inst] = IpRef(
            instance=inst,
            name=el.get("Name") or inst,
            version=el.get("Version") or "",
        )

    if not pins:
        raise DbError(f"{path}: 没解析出任何引脚，格式可能变了")
    return McuDb(name, pins, ips, Path(path))


def find_mode_file(root: Path, ip_name: str, version: str) -> Path | None:
    """给定 ``<IP Name= Version=>``，定位它的 ``*_Modes.xml``。"""
    p = root / "IP" / f"{ip_name}-{version}_Modes.xml"
    return p if p.exists() else None


__all__ = [
    "DbError",
    "IpRef",
    "McuDb",
    "PinCap",
    "find_mode_file",
]
