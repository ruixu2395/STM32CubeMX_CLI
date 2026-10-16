"""`.ioc` 读写内核 —— 不依赖 CubeMX，纯文本可单测。"""

from .escape import escape, unescape
from .project import (
    VIRTUAL_KEYS,
    IocProject,
    Peripheral,
    Pin,
    ProjectError,
    is_virtual_pin,
)
from .raw import IocDocument, IocFormatError
from .signals import (
    gpio_exti_line,
    gpio_mode_of,
    is_gpio_signal,
    is_pin_name,
    normalize_pin,
    parse_pin,
    pin_number,
    pin_suffix,
    signal_to_ip,
)

__all__ = [
    "VIRTUAL_KEYS",
    "IocDocument",
    "IocFormatError",
    "IocProject",
    "Peripheral",
    "Pin",
    "ProjectError",
    "escape",
    "gpio_exti_line",
    "gpio_mode_of",
    "is_gpio_signal",
    "is_pin_name",
    "is_virtual_pin",
    "normalize_pin",
    "parse_pin",
    "pin_number",
    "pin_suffix",
    "signal_to_ip",
    "unescape",
]
