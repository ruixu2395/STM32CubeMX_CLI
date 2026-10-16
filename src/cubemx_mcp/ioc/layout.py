"""`.ioc` 键空间里那几个**结构性**的键名。

只留真正被用到的：派生清单键（``Mcu.Pin*`` / ``Mcu.IP*``）与系统级伪外设。
JSON 中间态时期的字段映射（``PIN_TOP_LEVEL`` / ``passthrough_group`` 之类）
随 JSON 一起去掉了 —— 现在 `cubemx_mcp.ioc.project` 直接读写 `IocDocument`，
没有"字段映射"这一层。
"""

import re

from .signals import PIN_RE

#: 派生键：``Mcu.Pin<N>`` / ``Mcu.PinsNb``。引脚清单由 `IocProject` 维护，
#: 排序规则是**封装位号**（实测 CubeMX 就是这么写的）。
MCU_PIN_INDEX_RE = re.compile(r"^Mcu\.Pin(\d+)$")
MCU_PINS_NB_KEY = "Mcu.PinsNb"

#: 派生键：``Mcu.IP<N>`` / ``Mcu.IPNb``。外设清单由 `IocProject` 维护，
#: 排序规则是**字母序**（实测）。
MCU_IP_INDEX_RE = re.compile(r"^Mcu\.IP(\d+)$")
MCU_IP_NB_KEY = "Mcu.IPNb"

#: ``<PIN>.<属性>``。用 `signals.PIN_RE` 拼出来，才能认下
#: ``PC13-ANTI_TAMP.Signal`` 这种带后缀的引脚名（F407 上有 6 个）。
#:
#: 用**具名组**而不是编号组：``PIN_RE`` 自带两三个捕获组，套进来之后编号会整体
#: 后移，写 ``group(2)`` 拿到的是端口字母而不是属性名（踩过）。
PIN_PROP_RE = re.compile(rf"^(?P<pin>{PIN_RE.pattern[1:-1]})\.(?P<prop>.+)$")

#: CubeMX 无条件写进 ``Mcu.IP*`` 的系统级伪外设。它们的配置键落在各自的
#: 命名空间（``NVIC.*`` / ``RCC.*`` / ``Dma.*``），**不用 ``IPParameters``
#: 登记**，也不是"必须接引脚"的普通外设。校验时要把它们排除。
SYSTEM_IPS = frozenset({"NVIC", "RCC", "DMA"})


def is_derived_key(key: str) -> bool:
    """是否是引脚/外设清单的派生键（由 `IocProject` 生成，不该手工维护）。"""
    return bool(
        MCU_PIN_INDEX_RE.match(key)
        or MCU_IP_INDEX_RE.match(key)
        or key in (MCU_PINS_NB_KEY, MCU_IP_NB_KEY)
    )


def pin_prop(key: str) -> tuple[str, str] | None:
    """``"PA9.Signal"`` -> ``("PA9", "Signal")``；不是引脚属性则 None。"""
    m = PIN_PROP_RE.match(key)
    return (m.group("pin"), m.group("prop")) if m else None


__all__ = [
    "MCU_IP_INDEX_RE",
    "MCU_IP_NB_KEY",
    "MCU_PIN_INDEX_RE",
    "MCU_PINS_NB_KEY",
    "PIN_PROP_RE",
    "SYSTEM_IPS",
    "is_derived_key",
    "pin_prop",
]
