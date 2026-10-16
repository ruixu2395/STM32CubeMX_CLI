"""`.ioc` 的结构化活视图。

``Pin`` / ``Peripheral`` 都是**视图**，不持有自己的副本：读属性就是去
`IocDocument.entries` 里取键，写属性就是直接改那个字典。这样"视图和文件对不上"
这类同步 bug 从根上不存在 —— 只有一份真相。

.. code-block:: text

    PA9.Signal=USART1_TX   <--->   project.find_pin("PA9").signal

派生键（``Mcu.Pin0..N`` / ``Mcu.PinsNb`` / ``Mcu.IP0..N`` / ``Mcu.IPNb``）由本类
维护：新增引脚就追加 ``Mcu.Pin<N>`` 并更新 ``PinsNb``，移除时重排编号不留空洞。
调用方只表达"我要 PA2 做 USART2_TX"，编号的事不用管。

写盘走 ``临时文件 + os.replace`` 的原子替换，覆盖前自动备份。校验由
`cubemx_mcp.validate` 负责，本模块不做判断 —— 它只保证"内存里改对了，落盘就是
那个样子"。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from .layout import MCU_IP_INDEX_RE, MCU_IP_NB_KEY, MCU_PIN_INDEX_RE, MCU_PINS_NB_KEY
from .raw import IocDocument
from .signals import (
    PIN_RE,
    is_gpio_signal,
    normalize_pin,
    parse_pin,
    signal_to_ip,
)

#: 引脚上被提升为顶层属性的键（其余进 ``Pin.properties``）
_PIN_FIELDS = frozenset({"Signal", "Mode", "Locked", "GPIO_Label", "GPIOParameters"})

#: 引脚的"结构性"键：写在 ``<PIN>.`` 下，但**不进** ``GPIOParameters`` 登记表。
#: 其余属性（``GPIO_PuPd`` / ``GPIO_Speed`` / ``PinAttribute`` …）必须登记，
#: 否则 CubeMX 加载时会静默丢弃 —— 实测见 `Pin.set_property` 的说明。
PIN_STRUCTURAL_KEYS = frozenset({"Signal", "Mode", "Locked"})

#: 引脚参数登记表键名。与 ``<IP>.IPParameters`` 是同一套机制。
GPIO_PARAMETERS_KEY = "GPIOParameters"

#: 外设级的"模式"键。USART/UART/I2S/SAI/USB 用 ``VirtualMode``，SPI 用
#: ``VirtualType``。两者都是**独立于引脚 ``Mode``** 的键，必须成对维护。
VIRTUAL_KEYS = ("VirtualMode", "VirtualType")

#: 外设上被提升为顶层属性的键
_PER_FIELDS = frozenset({*VIRTUAL_KEYS, "IPParameters"})

#: 虚拟引脚前缀。``VP_SYS_VS_Systick`` 这类不占真实封装引脚，名字也不符合
#: ``P[A-Z]\d+`` 格式，必须单独放行。
VIRTUAL_PIN_PREFIX = "VP_"


def is_virtual_pin(name: str) -> bool:
    """``VP_SYS_VS_Systick`` 这类 CubeMX 内部引脚。"""
    return name.startswith(VIRTUAL_PIN_PREFIX)


class ProjectError(ValueError):
    """对工程的非法操作（引脚重名、外设不存在等）。"""


class Pin:
    """单个引脚的视图。"""

    __slots__ = ("_doc", "name")

    def __init__(self, doc: IocDocument, name: str) -> None:
        self._doc = doc
        self.name = name

    # ------------------------------------------------------------ 顶层属性

    @property
    def signal(self) -> str | None:
        """如 ``USART1_TX``；未配置返回 None。"""
        value = self._doc.get(f"{self.name}.Signal")
        return value or None

    @signal.setter
    def signal(self, value: str | None) -> None:
        self._doc.set(f"{self.name}.Signal", value or None)

    @property
    def mode(self) -> str | None:
        """如 ``Asynchronous``；与 ``VirtualMode`` 是两个独立的键。"""
        return self._doc.get(f"{self.name}.Mode") or None

    @mode.setter
    def mode(self, value: str | None) -> None:
        self._doc.set(f"{self.name}.Mode", value or None)

    @property
    def label(self) -> str | None:
        """``GPIO_Label``。未标注时 CubeMX 根本不写这个键，所以空串也算未标注。"""
        value = self._doc.get(f"{self.name}.GPIO_Label")
        return value or None

    @label.setter
    def label(self, value: str | None) -> None:
        # 走 set_property 而不是直接写键：标签必须登记进 GPIOParameters，
        # 否则 CubeMX 加载时会把这一行悄悄丢掉（实测过，见 set_property）。
        self.set_property("GPIO_Label", value or None)

    @property
    def locked(self) -> bool | None:
        value = self._doc.get(f"{self.name}.Locked")
        if value is None:
            return None
        return value.strip().lower() == "true"

    @locked.setter
    def locked(self, value: bool | None) -> None:
        self._doc.set(f"{self.name}.Locked", None if value is None else
                      ("true" if value else "false"))

    # ------------------------------------------------------------ 其余属性

    @property
    def properties(self) -> dict[str, str]:
        """除上述四个之外的 ``<PIN>.<Key>``（``GPIO_PuPd`` 之类）。"""
        prefix = f"{self.name}."
        out: dict[str, str] = {}
        for key in self._doc.keys_with_prefix(prefix):
            prop = key[len(prefix):]
            if prop not in _PIN_FIELDS:
                out[prop] = self._doc.get(key) or ""
        return out

    @property
    def parameters(self) -> tuple[str, ...]:
        """``GPIOParameters`` 里登记的属性名。

        **这是 CubeMX 认不认这一行的开关。** 实测：手工写下 ``PB0.GPIO_Label=LED``
        但不登记 ``PB0.GPIOParameters=GPIO_Label``，``config load`` → ``saveas``
        往返之后这一行会**消失**（``GPIO_PuPd`` / ``GPIO_Speed`` 同理，
        ``Locked`` 与 ``Signal`` 不受影响）。ST 自己发布的板级文件
        （``db/plugins/boardmanager/boards/*.ioc``）里，每个带 GPIO 参数的引脚
        都有这一行，正是为此。
        """
        raw = self._doc.get(f"{self.name}.{GPIO_PARAMETERS_KEY}")
        if not raw:
            return ()
        return tuple(p.strip() for p in raw.split(",") if p.strip())

    def add_parameter(self, name: str) -> bool:
        """把属性名登记进 ``GPIOParameters``；已存在返回 False。"""
        if name in PIN_STRUCTURAL_KEYS:
            return False
        current = list(self.parameters)
        if name in current:
            return False
        current.append(name)
        self._doc.set(f"{self.name}.{GPIO_PARAMETERS_KEY}", ",".join(current))
        return True

    def _drop_from_parameters(self, name: str) -> None:
        current = [p for p in self.parameters if p != name]
        if current:
            self._doc.set(f"{self.name}.{GPIO_PARAMETERS_KEY}", ",".join(current))
        else:
            self._doc.discard(f"{self.name}.{GPIO_PARAMETERS_KEY}")

    @property
    def values(self) -> dict[str, str]:
        """该引脚上**全部** ``<PIN>.<Key>``，含 ``Signal`` / ``Mode`` 这些顶层字段，
        但不含登记表本身。

        与 `properties` 的区别：后者排除顶层字段，适合"还剩哪些杂项"这种问法；
        要对登记表做完整核对（有没有登记了却没值的键）必须用这个。
        """
        prefix = f"{self.name}."
        return {
            key[len(prefix):]: self._doc.get(key) or ""
            for key in self._doc.keys_with_prefix(prefix)
            if key != f"{self.name}.{GPIO_PARAMETERS_KEY}"
        }

    def set_property(self, name: str, value: str | None) -> None:
        """设一个引脚属性并登记进 ``GPIOParameters``（设 None 则删除）。"""
        if value is None:
            self._doc.discard(f"{self.name}.{name}")
            self._drop_from_parameters(name)
        else:
            self._doc.set(f"{self.name}.{name}", value)
            self.add_parameter(name)

    def get_property(self, name: str) -> str | None:
        return self._doc.get(f"{self.name}.{name}")

    # ---------------------------------------------------------------- 杂项

    @property
    def configured(self) -> bool:
        """是否有任何实际配置（区别于只有 Locked 之类的空壳）。"""
        return bool(self.signal or self.mode or self.label or self.properties)

    def clear_config(self) -> None:
        """抹掉这个引脚的全部配置键（不从工程里摘除）。"""
        prefix = f"{self.name}."
        for key in self._doc.keys_with_prefix(prefix):
            self._doc.discard(key)

    def as_dict(self) -> dict[str, object]:
        """给 ``--json`` 输出用。"""
        return {
            "name": self.name,
            "signal": self.signal,
            "mode": self.mode,
            "label": self.label,
            "locked": self.locked,
            "parameters": list(self.parameters),
            "properties": self.properties,
        }

    def __repr__(self) -> str:
        bits = [self.name]
        if self.signal:
            bits.append(self.signal)
        if self.mode:
            bits.append(f"({self.mode})")
        if self.label:
            bits.append(f"label={self.label}")
        return f"<Pin {' '.join(bits)}>"


class Peripheral:
    """单个外设实例的视图（``USART1``）。"""

    __slots__ = ("_doc", "name")

    def __init__(self, doc: IocDocument, name: str) -> None:
        self._doc = doc
        self.name = name

    @property
    def virtual_mode(self) -> str | None:
        """``VM_ASYNC`` 之类。只有 USART/UART/I2S/SAI/USB 有这个概念。"""
        return self._doc.get(f"{self.name}.VirtualMode") or None

    @virtual_mode.setter
    def virtual_mode(self, value: str | None) -> None:
        self.set_virtual("VirtualMode", value)

    @property
    def virtual_type(self) -> str | None:
        """``VM_MASTER`` 之类。**SPI 专用** —— 它不用 ``VirtualMode``。"""
        return self._doc.get(f"{self.name}.VirtualType") or None

    @virtual_type.setter
    def virtual_type(self, value: str | None) -> None:
        self.set_virtual("VirtualType", value)

    @property
    def virtual(self) -> tuple[str, str] | None:
        """当前生效的 ``(键名, 值)``；两个都没设则为 None。"""
        for key in VIRTUAL_KEYS:
            value = self._doc.get(f"{self.name}.{key}")
            if value:
                return key, value
        return None

    def set_virtual(self, key: str, value: str | None) -> None:
        """设置外设级模式键，并清掉另一个（``VirtualMode``/``VirtualType`` 互斥）。"""
        if key not in VIRTUAL_KEYS:
            raise ProjectError(f"{key} 不是外设级模式键，只能是 {VIRTUAL_KEYS}")
        for other in VIRTUAL_KEYS:
            if other != key:
                self._doc.discard(f"{self.name}.{other}")
                self._drop_from_parameters(other)
        if value is None:
            self._doc.discard(f"{self.name}.{key}")
            self._drop_from_parameters(key)
        else:
            self._doc.set(f"{self.name}.{key}", value)
            self.add_parameter(key)

    @property
    def parameters(self) -> tuple[str, ...]:
        """``IPParameters`` 里登记的、**显式设置过**的参数名。

        CubeMX 只认这里列出的参数；写了 ``USART2.BaudRate`` 却没登记进
        ``IPParameters``，打开工程时会被无声丢掉。
        """
        raw = self._doc.get(f"{self.name}.IPParameters")
        if not raw:
            return ()
        return tuple(p.strip() for p in raw.split(",") if p.strip())

    def add_parameter(self, name: str) -> bool:
        """把参数名登记进 ``IPParameters``；已存在返回 False。"""
        current = list(self.parameters)
        if name in current:
            return False
        current.append(name)
        self._doc.set(f"{self.name}.IPParameters", ",".join(current))
        return True

    def _drop_from_parameters(self, name: str) -> None:
        current = [p for p in self.parameters if p != name]
        if current:
            self._doc.set(f"{self.name}.IPParameters", ",".join(current))
        else:
            self._doc.discard(f"{self.name}.IPParameters")

    @property
    def properties(self) -> dict[str, str]:
        """除 ``VirtualMode`` / ``IPParameters`` 外的 ``<IP>.<Key>``。"""
        prefix = f"{self.name}."
        out: dict[str, str] = {}
        for key in self._doc.keys_with_prefix(prefix):
            prop = key[len(prefix):]
            if prop not in _PER_FIELDS:
                out[prop] = self._doc.get(key) or ""
        return out

    @property
    def values(self) -> dict[str, str]:
        """该外设上**全部** ``<IP>.<Key>``，含 ``VirtualMode`` 这类顶层字段，
        但不含登记表 ``IPParameters`` 本身。"""
        prefix = f"{self.name}."
        return {
            key[len(prefix):]: self._doc.get(key) or ""
            for key in self._doc.keys_with_prefix(prefix)
            if key != f"{self.name}.IPParameters"
        }

    def get_param(self, name: str) -> str | None:
        return self._doc.get(f"{self.name}.{name}")

    def set_param(self, name: str, value: str | None) -> None:
        """设一个参数并自动登记进 ``IPParameters``（设 None 则删除）。"""
        if value is None:
            self._doc.discard(f"{self.name}.{name}")
            self._drop_from_parameters(name)
        else:
            self._doc.set(f"{self.name}.{name}", value)
            self.add_parameter(name)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "virtual": {self.virtual[0]: self.virtual[1]} if self.virtual else {},
            "parameters": list(self.parameters),
            "properties": self.properties,
        }

    def __repr__(self) -> str:
        return f"<Peripheral {self.name} {self.virtual_mode or ''}>".strip()


class IocProject:
    """一份 `.ioc` 工程。"""

    def __init__(
        self,
        doc: IocDocument,
        source: Path | None = None,
        device: object | None = None,
    ) -> None:
        self.doc = doc
        self.source = source
        #: 可选的 `cubemx_mcp.db.Device`。有它才能按封装位号排序引脚
        #: （CubeMX 就是这么写的），也能用权威数据做校验。
        self.device = device

    @classmethod
    def load(cls, path: str | Path, device: object | None = None) -> IocProject:
        p = Path(path)
        return cls(IocDocument.load(p), source=p, device=device)

    @property
    def path(self) -> Path | None:
        return self.source

    # ------------------------------------------------------------ MCU 信息

    @property
    def mcu_name(self) -> str:
        """``Mcu.Name``，如 ``STM32F407Z(E-G)Tx``。**它就是数据库的文件名。**"""
        return self.doc.get("Mcu.Name") or ""

    @property
    def package(self) -> str | None:
        return self.doc.get("Mcu.Package") or None

    @property
    def cpn(self) -> str | None:
        """`Mcu.CPN`，如 ``STM32F407ZGT6``。"""
        return self.doc.get("Mcu.CPN") or None

    # ---------------------------------------------------------------- 引脚

    def _pin_order(self) -> list[str]:
        """``Mcu.Pin0..N`` 给出的引脚顺序（加入顺序，非字母序）。"""
        indexed: list[tuple[int, str]] = []
        for key in self.doc.entries:
            m = MCU_PIN_INDEX_RE.match(key)
            if m:
                indexed.append((int(m.group(1)), self.doc.get(key) or ""))
        indexed.sort()
        return [name for _, name in indexed if name]

    def pins(self) -> tuple[Pin, ...]:
        return tuple(Pin(self.doc, name) for name in self._pin_order())

    def pin_names(self) -> tuple[str, ...]:
        return tuple(self._pin_order())

    def resolve_pin(self, name: str) -> str:
        """把用户给的引脚名补全成**库里的正式写法**。

        ``PA0`` 在库里叫 ``PA0-WKUP``、``PC13`` 叫 ``PC13-ANTI_TAMP``（F407 上有
        6 个这种脚）。用户报裸名时按「同端口同脚号」唯一匹配补全；匹配不到或有
        歧义就原样返回，由校验层去报 ``pin.unknown``。

        虚拟引脚（``VP_SYS_VS_Systick``）**大小写敏感，原样返回** ——
        ``normalize_pin`` 会把它们大写掉，那就不认识了。
        """
        n = name.strip()
        if is_virtual_pin(n.upper()):
            return n
        n = normalize_pin(n)
        if self.device is None or n in self.device.mcu.pins:
            return n
        matches = [
            p for p in self.device.mcu.pins
            if PIN_RE.match(p) and parse_pin(p) == parse_pin(n)
        ]
        return matches[0] if len(matches) == 1 else n

    def find_pin(self, name: str) -> Pin | None:
        """按名字找引脚。虚拟引脚（``VP_...``）按原样匹配，不做正则规整。"""
        try:
            target = self.resolve_pin(name)
        except ValueError:
            return None
        return Pin(self.doc, target) if target in self._pin_order() else None

    def has_pin(self, name: str) -> bool:
        """工程清单里是否登记了这个引脚。"""
        return self.find_pin(name) is not None

    def add_pin(self, name: str) -> Pin:
        """把引脚登记进 ``Mcu.Pin*`` 清单并返回视图。已存在则直接返回。

        名字先过 `resolve_pin` 补全 —— 否则 ``add_pin("PC13")`` 会往工程里写一个
        CubeMX 根本不认识的 ``PC13``（库里叫 ``PC13-ANTI_TAMP``）。
        """
        n = self.resolve_pin(name)
        order = self._pin_order()
        if n in order:
            return Pin(self.doc, n)
        order.append(n)
        self._reindex_pins(order)
        return Pin(self.doc, n)

    def remove_pin(self, name: str) -> None:
        """从清单里摘掉引脚，并抹掉它的全部配置键。"""
        n = self.resolve_pin(name)
        order = [p for p in self._pin_order() if p != n]
        self._reindex_pins(order)
        Pin(self.doc, n).clear_config()

    def _pin_position(self, name: str) -> int | None:
        """该引脚在封装里的位号；库里没有（虚拟引脚）返回 None。"""
        if self.device is None:
            return None
        cap = self.device.mcu.pins.get(name)
        return cap.position if cap is not None else None

    def _reindex_pins(self, order: list[str]) -> None:
        """把引脚清单写回 ``Mcu.Pin0..N``。

        **有数据库时按封装位号排序** —— 实测三条 CubeMX 生成的基准文件（
        PC13(7),PA0(34),PB0(46),PB1(47),PA9(101),PA10(102) 等）全部吻合，
        而不是加入顺序或字母序。库里查不到的（虚拟引脚）稳定地排在最后。
        """
        if self.device is not None:
            # sorted() 是稳定的，所以查不到位号的会保持原有相对顺序并排在最后
            order = sorted(
                order,
                key=lambda name: (1, 0) if self._pin_position(name) is None
                else (0, self._pin_position(name)),
            )

        for key in [k for k in self.doc.entries if MCU_PIN_INDEX_RE.match(k)]:
            self.doc.discard(key)
        for i, pin_name in enumerate(order):
            self.doc.set(f"Mcu.Pin{i}", pin_name)
        self.doc.set(MCU_PINS_NB_KEY, str(len(order)))

    # ---------------------------------------------------------------- 外设

    def _ip_order(self) -> list[str]:
        indexed: list[tuple[int, str]] = []
        for key in self.doc.entries:
            m = MCU_IP_INDEX_RE.match(key)
            if m:
                indexed.append((int(m.group(1)), self.doc.get(key) or ""))
        indexed.sort()
        return [name for _, name in indexed if name]

    def peripherals(self) -> tuple[Peripheral, ...]:
        return tuple(Peripheral(self.doc, name) for name in self._ip_order())

    def peripheral_names(self) -> tuple[str, ...]:
        return tuple(self._ip_order())

    def find_peripheral(self, name: str) -> Peripheral | None:
        key = name.strip()
        return Peripheral(self.doc, key) if key in self._ip_order() else None

    def add_peripheral(self, name: str) -> Peripheral:
        """登记一个外设实例。已存在则直接返回。"""
        key = name.strip()
        order = self._ip_order()
        if key in order:
            return Peripheral(self.doc, key)
        order.append(key)
        self._reindex_ips(order)
        return Peripheral(self.doc, key)

    def remove_peripheral(self, name: str) -> None:
        key = name.strip()
        order = [p for p in self._ip_order() if p != key]
        self._reindex_ips(order)
        prefix = f"{key}."
        for k in self.doc.keys_with_prefix(prefix):
            self.doc.discard(k)

    def _reindex_ips(self, order: list[str]) -> None:
        """写回 ``Mcu.IP0..N``。**按字母序** —— 实测 CubeMX 生成的
        ``DMA, I2C1, NVIC, RCC, SYS, USART1`` 正是字母序，而非添加顺序
        （DMA 是用户加的却排在自动加的 NVIC/RCC 前面）。
        """
        for key in [k for k in self.doc.entries if MCU_IP_INDEX_RE.match(k)]:
            self.doc.discard(key)
        for i, ip_name in enumerate(sorted(order)):
            self.doc.set(f"Mcu.IP{i}", ip_name)
        self.doc.set(MCU_IP_NB_KEY, str(len(order)))

    # ---------------------------------------------------------------- 查询

    def owner_of(self, pin: Pin) -> str | None:
        """这个引脚当前归属哪个外设（``GPIO_Output`` / ``GPXTI0`` 返回 None）。

        有数据库时用**该 MCU 的权威外设实例名表**精确匹配；没有才退到后缀
        剥离的启发式（后者对 ``ETH_RXD0`` 会误判）。
        """
        signal = pin.signal
        if not signal:
            return None
        if self.device is not None:
            return self.device.mcu.signal_owner(signal)
        return signal_to_ip(signal, frozenset(self._ip_order()))

    def gpio_pins(self) -> tuple[Pin, ...]:
        return tuple(p for p in self.pins() if p.signal and is_gpio_signal(p.signal))

    def passthrough_keys(self) -> tuple[str, ...]:
        """不归本类管理的键（``NVIC.*``、``RCC.*``、``ProjectManager.*`` …）。

        它们原样保留在 `doc` 里，本模块从不丢弃。
        """
        owned = set(self._ip_order()) | set(self._pin_order())
        out = []
        for key in self.doc.entries:
            if MCU_PIN_INDEX_RE.match(key) or MCU_IP_INDEX_RE.match(key):
                continue
            if key in (MCU_PINS_NB_KEY, MCU_IP_NB_KEY):
                continue
            head, sep, _ = key.partition(".")
            if sep and head in owned:
                continue
            out.append(key)
        return tuple(out)

    # ---------------------------------------------------------------- 落盘

    def render(self) -> str:
        return self.doc.render()

    def backup(self, path: str | Path | None = None) -> Path | None:
        """给现有文件留一份带时间戳的副本；文件不存在则返回 None。

        时间戳只到秒，所以同一秒内的**第二次**写入会算出同一个文件名。这里往
        后找第一个空位（``-2``、``-3``…）而不是直接覆盖 —— 覆盖等于悄悄丢掉
        上一份备份，而"每次落盘都留得回退点"正是这个库的卖点。MCP 层让 AI 连续
        改的时候，同一秒两次写入是常态，不是边角情况。
        """
        target = Path(path) if path else self.source
        if target is None or not target.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = target.with_name(f"{target.name}.bak-{stamp}")
        nth = 1
        while bak.exists():
            nth += 1
            bak = target.with_name(f"{target.name}.bak-{stamp}-{nth}")
        bak.write_bytes(target.read_bytes())
        return bak

    def save(self, path: str | Path | None = None, *, backup: bool = True) -> Path:
        """原子写回：先写同目录临时文件，再 ``os.replace`` 覆盖。

        ``os.replace`` 在同一文件系统上是原子的，所以要么看到旧文件、要么看到
        新文件，不会出现写了一半的 `.ioc`。校验失败时调用方**不调用本方法**，
        原文件因此一个字节都不会变。
        """
        target = Path(path) if path else self.source
        if target is None:
            raise ProjectError("没有目标路径：既未指定也非 load 而来")
        if backup and target.exists():
            self.backup(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.render()
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding=self.doc.encoding, newline="") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.source = target
        return target

    def __repr__(self) -> str:
        return (
            f"IocProject({self.mcu_name!r}, "
            f"{len(self._pin_order())} pins, {len(self._ip_order())} ips)"
        )
