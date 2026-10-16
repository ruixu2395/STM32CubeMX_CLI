"""中断表：``db/mcu/IP/NVIC-<MCU>_Modes.xml``。

CubeMX「NVIC Settings」页里的勾选框 = ``.ioc`` 里的 ``NVIC.<IRQn>=`` 一行。
要开哪个中断、优先级能填到几，都得先知道**这颗 MCU 上有哪些 IRQ、各自属于谁** ——
这份文件就是权威来源，但它的形态和别的 ``*_Modes.xml`` 完全不同：

* **一个 ``<RefMode>`` 都没有**（``<Mode>`` 也是）。所以 `ModeDb` 解析不了它
  （会走 ``if not modes: raise DbError`` 那条路），必须另起一个读取器。
* 真正的表是那个**隐藏的参数** ``<RefParameter Name="IRQn" Visible="false">``，
  下面挂着几十上百条 ``<PossibleValue>``，值是一串**冒号分隔的元组**：

  ```
  Value="USART1_IRQn:Y:USART1::"
         └ IRQ 名  └ 标志 └ 归属外设 └ HAL 处理函数 └ 附加
  ```

  ``field[2]`` 就是「这个中断属于哪个外设」—— 可以直接查，不用拼字符串猜。

**``field[2]` 可能是逗号分隔的多个**（实测 ``TIM1_UP_TIM10_IRQn:Y:TIM1,TIM10``、
``EXTI9_5_IRQn:Y:EXTI5,EXTI6,EXTI7,EXTI8,EXTI9``、``SPI2_IRQn:Y:SPI2,I2S2``）。
漏了这个 ``split(",")``，TIM1/SPI3/USART 这类共享中断向量的外设就会**漏掉自己的中断**，
而且是静默漏。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .xml import DbError, child, children, descendants, parse

#: IRQ 值元组最少要有几段（名字/标志/归属）。后面的段历史上有过缺省。
MIN_FIELDS = 3


@dataclass(frozen=True)
class Irq:
    """一条中断定义。"""

    name: str
    #: ``<PossibleValue Comment="...">`` —— GUI 里显示的那句话
    comment: str | None = None
    #: field[1] 原样：``Y`` / ``N,W1`` / ``Y,DMA``
    flags: str = ""
    #: field[2] 按 ``,`` 拆开；没有归属时为空元组
    owners: tuple[str, ...] = ()
    #: field[3]：``HAL_GPIO_EXTI_IRQHandler`` 之类
    handler: str = ""
    #: field[4]：``0,0`` / ``DMA2`` / ``CSSEnabled``
    extra: str = ""
    #: 该中断只有在某个 semaphore 成立时才存在。条件表达式是 CubeMX 私有语法，
    #: 这里不求值，只原样带出来供展示。
    condition: str | None = None

    def belongs_to(self, instance: str) -> bool:
        return instance in self.owners


class IrqDb:
    """一份 ``NVIC-<MCU>_Modes.xml`` 的解析结果。"""

    def __init__(
        self,
        irqs: dict[str, Irq],
        priority_group: str | None = None,
        ranges: dict[str, tuple[int, int, int, int]] | None = None,
        path: Path | None = None,
    ) -> None:
        self.irqs = irqs
        #: ``<RefParameter Name="PriorityGroup">`` 的 DefaultValue
        self.priority_group = priority_group
        #: 分组名 -> ``(抢占下限, 抢占上限, 子优先级下限, 子优先级上限)``
        self.ranges = ranges or {}
        self.path = path

    @classmethod
    def load(cls, path: str | Path) -> IrqDb:
        p = Path(path)
        if not p.exists():
            raise DbError(f"中断表不存在: {p}")
        return _load_cached(str(p))

    # ---------------------------------------------------------------- 查询

    def get(self, name: str) -> Irq | None:
        return self.irqs.get(name)

    def all(self) -> tuple[Irq, ...]:
        """全部中断，按文件顺序。"""
        return tuple(self.irqs.values())

    def for_instance(self, instance: str) -> tuple[Irq, ...]:
        """归属于某个外设实例的中断，按文件顺序。

        共享中断向量会**同时**属于多个外设（``TIM1_UP_TIM10_IRQn`` 既属 TIM1 也属
        TIM10），两边都查得到。
        """
        return tuple(i for i in self.irqs.values() if i.belongs_to(instance))

    def for_stream(self, stream: str) -> Irq | None:
        """DMA stream 的中断（``DMA2_Stream2`` -> ``DMA2_Stream2_IRQn``）。

        走**查表**而不是拼字符串 —— 拼错了会静默变成"这个 stream 没有中断"。
        """
        return self.irqs.get(f"{stream}_IRQn")

    def priority_range(self, group: str | None = None) -> tuple[int, int, int, int]:
        """``(抢占下限, 抢占上限, 子优先级下限, 子优先级上限)``。

        找不到时返回 ``(0, 15, 0, 15)`` 这个宽范围，**不猜一个具体的分组** ——
        宁可放过也不误杀。
        """
        key = group or self.priority_group
        if key and key in self.ranges:
            return self.ranges[key]
        return (0, 15, 0, 15)

    def __repr__(self) -> str:
        return f"IrqDb({len(self.irqs)} irqs, group={self.priority_group})"


def _split_irq(value: str) -> Irq | None:
    """``"USART1_IRQn:Y:USART1::"`` -> `Irq`。段数不够就返回 None。"""
    fields = value.split(":")
    if len(fields) < MIN_FIELDS or not fields[0]:
        return None
    owners = tuple(o for o in fields[2].split(",") if o)
    return Irq(
        name=fields[0],
        flags=fields[1],
        owners=owners,
        handler=fields[3] if len(fields) > 3 else "",
        extra=fields[4] if len(fields) > 4 else "",
    )


def _int_attr(el, name: str) -> int | None:
    raw = el.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _condition_of(el) -> str | None:
    c = child(el, "Condition")
    if c is None:
        return None
    return c.get("Expression") or None


@lru_cache(maxsize=8)
def _load_cached(path: str) -> IrqDb:
    root = parse(path)

    irqs: dict[str, Irq] = {}
    priority_group: str | None = None
    #: ``(条件表达式, 是否带 FreeRTOS)`` -> ``{"pre": (min,max), "sub": (min,max)}``
    bounds: dict[tuple[str, bool], dict[str, tuple[int, int]]] = {}

    for el in descendants(root, "RefParameter"):
        name = el.get("Name")

        if name == "IRQn":
            for v in children(el, "PossibleValue"):
                raw = v.get("Value")
                if not raw:
                    continue
                irq = _split_irq(raw)
                if irq is None:
                    continue
                prev = irqs.get(irq.name)
                irqs[irq.name] = Irq(
                    name=irq.name,
                    comment=v.get("Comment") or (prev.comment if prev else None),
                    flags=irq.flags,
                    owners=irq.owners or (prev.owners if prev else ()),
                    handler=irq.handler,
                    extra=irq.extra,
                    condition=v.get("Condition") or (prev.condition if prev else None),
                )
            continue

        if name == "PriorityGroup":
            if el.get("DefaultValue"):
                priority_group = priority_group or el.get("DefaultValue")
            continue

        if name not in ("PreemptionPriority", "SubPriority"):
            continue
        expr = _condition_of(el)
        lo, hi = _int_attr(el, "Min"), _int_attr(el, "Max")
        if not expr or lo is None or hi is None:
            continue
        # 同一个分组常声明两条：一条条件里带 ``S_FREERTOS``（只在用了 RTOS 时成立），
        # 一条不带。分开存，取值时优先要不带 FreeRTOS 的那条 —— 报窄了会让用户
        # 以为 7 就是上限。
        slot = "pre" if name == "PreemptionPriority" else "sub"
        bounds.setdefault((expr, "FREERTOS" in expr), {"pre": (0, 15), "sub": (0, 15)})[slot] = (lo, hi)

    if not irqs:
        raise DbError(f"{path}: 没解析出中断表（IRQn 参数），格式可能变了")

    ranges: dict[str, tuple[int, int, int, int]] = {}
    for (expr, _freertos), b in bounds.items():
        ranges.setdefault(
            expr, (b["pre"][0], b["pre"][1], b["sub"][0], b["sub"][1])
        )
    # 无 FreeRTOS 的那条赢 —— setdefault 的顺序不保证，所以再走一遍覆盖。
    for (expr, freertos), b in bounds.items():
        if not freertos:
            ranges[expr] = (b["pre"][0], b["pre"][1], b["sub"][0], b["sub"][1])

    return IrqDb(irqs, priority_group, ranges, Path(path))


__all__ = ["DbError", "Irq", "IrqDb"]
