"""引脚名与 signal 名的解析。

`.ioc` 里一个引脚的归属靠两处表达：``PA9.Signal=USART1_TX`` 和
``Mcu.IP3=USART1``。要从 signal 名反推外设名（"USART1_TX" → "USART1"），
最可靠的办法是拿工程已知外设名做匹配；匹配不到时用后缀剥离的启发式兜底。

启发式覆盖常见外设，但对少数冷门 signal（如 ``FMC_SDNRAS``）会退化成
"整个名字都是外设名"。调用方可以把权威外设名列表传进来（M3 从 CubeMX 的
``db/mcu/<MCU>.xml`` 取），届时就是精确匹配。
"""

import re

#: 引脚名。**可以带后缀** —— CubeMX 与 MCU 库都用 ``PA0-WKUP``、``PC13-ANTI_TAMP``
#: 这种写法（F407 上有 6 个），正则不能只认裸 ``PA0``。
PIN_RE = re.compile(r"^P([A-Z])(\d{1,2})(?:-([A-Z0-9_]+))?$")

#: GPIO 引脚在 `.ioc` 里的 signal 写法。``GPIO_Output`` / ``GPIO_Analog`` …
_GPIO_MODE_RE = re.compile(r"^GPIO_([A-Za-z][A-Za-z0-9_]*)$")

#: EXTI 是特例：CubeMX 写 ``GPXTI0``（``GPIO`` + 脚号），**这个字符串在任何一份
#: MCU 库里都查不到**，是它自己合成出来的。所以合法性只能靠「该脚 ``IOModes``
#: 含 ``EXTI``」+「编号与该脚号一致」来判断。
_GPIO_EXTI_RE = re.compile(r"^GPXTI(\d{1,2})$")

# signal 名里表示"功能"的尾段 token。剥离这些就能露出外设名。
_FUNC_WORDS = frozenset(
    {
        # SAI 的 SD_A / SD_B 只有单字母尾段。不放开 C..F，避免过度剥离外设名。
        "A", "B",
        "TX", "RX", "CK", "SCL", "SDA", "SCK", "MISO", "MOSI", "NSS", "CS",
        "BKIN", "BIN", "NSS", "TXD", "RXD",
        "ETR", "DE", "RE", "CTS", "RTS", "IN", "OUT", "REF", "CLK", "CLKIN",
        "RMII", "MII", "ULPI", "VBUS", "ID", "DM", "DP", "SOF", "PWR",
        "OVERCURRENT", "OVCUR", "PPS", "MCO", "OSC", "OSC32", "SMBA", "SMBS",
        "ALARM", "TS", "TAMP", "WKUP", "STDBY", "SD", "SDI", "SDO", "FSYNC",
        "EXTI", "MDIO", "MDC", "CRS", "COL", "SWCLK", "SWDIO", "JTCK", "JTMS",
        "JTDI", "JTDO", "NJTRST", "TRACECLK", "TRACED0", "TRACED1", "TRACED2",
        "TRACED3", "SWO", "WAKE", "RST", "BOOT", "DIR", "NXT", "STP",
        "BK1", "BK2", "BK1_IO", "BK2_IO", "NCS", "IO",
    }
)

# 带通道号的尾段：CH2 / D0 / A15 / IN3 / R0 / MCO1 / EXTI9 / BK1_NCS …
_INDEXED_FUNC_RE = re.compile(
    r"^(?:CH|IN|OUT|IO|BK|NCS|MCO|EXTI|WKUP|D|A|B|C|R|G|T)?\d+[A-Z]?$"
)


def is_pin_name(name: str) -> bool:
    """``PA9`` / ``PA0-WKUP`` / ``PC13-ANTI_TAMP`` 之类是否为合法引脚名。"""
    return bool(PIN_RE.match(name))


def parse_pin(name: str) -> tuple[str, int]:
    """``"PA9"`` -> ``("A", 9)``。后缀（``-WKUP``）忽略。"""
    m = PIN_RE.match(name)
    if not m:
        raise ValueError(f"非法引脚名: {name!r}")
    return m.group(1), int(m.group(2))


def pin_number(name: str) -> int:
    """``"PA0-WKUP"`` -> ``0``。"""
    return parse_pin(name)[1]


def pin_suffix(name: str) -> str | None:
    """``"PA0-WKUP"`` -> ``"WKUP"``；裸 ``PA9`` 返回 None。"""
    m = PIN_RE.match(name)
    return m.group(3) if m else None


def normalize_pin(name: str) -> str:
    """统一成大写并校验。

    只做大小写与空白规整，**不补后缀** —— ``PA0`` 与 ``PA0-WKUP`` 在库里是两个
    不同写法，要带后缀请交给 `cubemx_mcp.ops.resolve_pin` 用数据库补全。
    """
    n = name.strip().upper()
    if not is_pin_name(n):
        raise ValueError(f"非法引脚名: {name!r}")
    return n


def _is_func_token(token: str) -> bool:
    return token in _FUNC_WORDS or bool(_INDEXED_FUNC_RE.match(token))


def gpio_mode_of(signal: str) -> str | None:
    """GPIO 写法 -> 引脚功能名。

    >>> gpio_mode_of("GPIO_Output")
    'Output'
    >>> gpio_mode_of("GPXTI5")
    'EXTI'
    >>> gpio_mode_of("USART1_TX")
    """
    m = _GPIO_MODE_RE.match(signal)
    if m:
        return m.group(1)
    if _GPIO_EXTI_RE.match(signal):
        return "EXTI"
    return None


def gpio_exti_line(signal: str) -> int | None:
    """``"GPXTI5"`` -> ``5``；不是 EXTI 写法返回 None。"""
    m = _GPIO_EXTI_RE.match(signal)
    return int(m.group(1)) if m else None


def is_gpio_signal(signal: str) -> bool:
    """``GPIO_Output`` / ``GPXTI0`` 这类"signal 即引脚功能"的写法。"""
    return gpio_mode_of(signal) is not None


def signal_to_ip(signal: str, known_ips: frozenset[str] | set[str] | None = None) -> str | None:
    """从 signal 名推断所属外设名。

    返回 None 表示该 signal 不属于任何外设（``GPIO_*`` 引脚模式）。

    >>> signal_to_ip("USART1_TX")
    'USART1'
    >>> signal_to_ip("USB_OTG_FS_VBUS")
    'USB_OTG_FS'
    >>> signal_to_ip("GPIO_Output")
    """
    if not signal:
        return None
    # GPIO 的 signal 名本身就是引脚功能（GPIO_Input / GPIO_Output / GPXTI0 …），
    # 不属于任何可配置外设
    if is_gpio_signal(signal):
        return None

    if known_ips:
        # 精确匹配优先：找已知外设名里最长的、作为 signal 前缀的那个
        best = None
        for ip in known_ips:
            if (signal == ip or signal.startswith(ip + "_") or signal.startswith(ip + "-")) and (
                best is None or len(ip) > len(best)
            ):
                best = ip
        if best is not None:
            return best

    # 启发式：从右侧不断剥掉功能尾段
    s = signal
    while True:
        idx = max(s.rfind("_"), s.rfind("-"))
        if idx <= 0:
            break
        tail = s[idx + 1:]
        if not _is_func_token(tail):
            break
        s = s[:idx]
    return s
