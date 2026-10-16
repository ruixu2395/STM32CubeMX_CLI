"""外设模式库：``db/mcu/IP/<IP Name>-<IP Version>_Modes.xml``。

这份文件回答三个问题，每个都对应一层校验：

1. **这个模式需要哪些信号？**
   ``<Mode Name="Synchronous"><SignalLogicalOp Name="AND">CK, RX, TX``
   —— 所以配 Synchronous 却只给了 TX/RX 是错的。
2. **这个模式对应哪个 ``VirtualMode``？**
   ``<RefMode Name="Asynchronous"><Parameter Name="VirtualMode">
   <PossibleValue>VM_ASYNC`` —— 这是 ``.ioc`` 里两个**独立**的键
   （``PA9.Mode`` 与 ``USART1.VirtualMode``），必须一起改才不会打架。
3. **这个模式允许设置哪些参数？** ``<RefMode>`` 里的 ``<Parameter Name>``
   列表，沿 ``BaseMode`` 递归继承（``Asynchronous`` 的 ``BaudRate`` 来自
   ``usartBasic``）。

注意 ``VirtualMode`` **不是所有外设都有** —— 全库只有 USART/UART/I2S/SAI/
USB 这几个用。没有的时候 `ModeDb.virtual_mode` 返回 None，调用方就不写这个键。

另一处刻意的宽松：``RefParameter`` 常按 ``<Condition>`` 重复多次（同一个
``WordLength`` 在 ``VM_ASYNC`` 和 ``VM_IRDA`` 下取值不同）。条件表达式是
CubeMX 私有语法，本工具不去求值，而是把同名参数的取值**并集**当作合法集 ——
宁可漏报也不误杀用户正确的配置。

**默认值取"不带条件"的那条声明**，不是取第一条。上面这份文件的实测：

    Parity     PARITY_EVEN  cond= VirtualMode = VM_SMARTCARD
               PARITY_NONE  cond= None            ← 真默认
    HwFlowCtl  UART_HWCONTROL_RTS  cond= $IpInstance_R
               UART_HWCONTROL_CTS  cond= $IpInstance_T
               UART_HWCONTROL_NONE cond= None     ← 真默认

带条件的那几条只在特定模式下成立，当成默认值报出去会让用户以为
"Parity 默认是偶校验"。取值**并集**那条宽松策略不受影响，只改 `default`。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from .xml import DbError, child, children, descendants, parse


@dataclass(frozen=True)
class ParamDef:
    """一个参数的定义（同名多条已合并）。"""

    name: str
    default: str | None = None
    group: str | None = None
    type: str | None = None
    values: tuple[str, ...] = ()
    minimum: str | None = None
    maximum: str | None = None
    comment: str | None = None

    @property
    def is_list(self) -> bool:
        return self.type == "list" or bool(self.values)


#: 外设级的"模式"键。CubeMX 用了两个名字：USART/UART/I2S/SAI/USB 写
#: ``VirtualMode``，SPI 写 ``VirtualType``。它们和引脚上的 ``Mode`` 是**两个
#: 独立的键**，配一个必须同时配另一个，否则工程自相矛盾。
VIRTUAL_KEYS = ("VirtualMode", "VirtualType")


@dataclass(frozen=True)
class ModeDef:
    """一个可选的引脚模式。"""

    name: str
    abstract: bool = False
    base: str | None = None
    #: ``<Mode UserName="...">``。**有它才是 GUI 里真能选的一项** ——
    #: ``Enable_Timer`` 这种内部伪模式没有 UserName。虚拟引脚该写哪个模式名
    #: 全靠这个区分（见 `ModeDb.virtual_pin_mode`）。
    user_name: str | None = None
    #: 该模式对应的外设级模式键（``VirtualMode`` 或 ``VirtualType``）；多数
    #: 外设两者皆无，此时为 None，不该往 `.ioc` 里写这个键
    virtual_key: str | None = None
    #: 该键应取的值，如 ``VM_ASYNC`` / ``VM_MASTER``
    virtual_values: tuple[str, ...] = ()
    #: 必需信号（``SignalLogicalOp`` 的子 ``Signal``），无定义时为 None
    signals: tuple[str, ...] | None = None
    #: "AND"（全部需要）或 "OR"（任一即可）
    signal_op: str = "AND"
    #: 该模式允许的参数名（已展开 BaseMode 继承）
    params: tuple[str, ...] = ()

    @property
    def virtual(self) -> tuple[str, str] | None:
        """``("VirtualMode", "VM_ASYNC")``；没有这个概念时 None。"""
        if self.virtual_key and self.virtual_values:
            return self.virtual_key, self.virtual_values[0]
        return None

    @property
    def virtual_mode(self) -> str | None:
        """``VirtualMode`` 的值（SPI 那种用 ``VirtualType`` 的返回 None）。"""
        v = self.virtual
        return v[1] if v and v[0] == "VirtualMode" else None


class ModeDb:
    """一份 ``*_Modes.xml`` 的解析结果。"""

    def __init__(
        self,
        modes: dict[str, ModeDef],
        params: dict[str, ParamDef],
        path: Path | None = None,
        virtual_defaults: dict[str, str] | None = None,
        vs_signals: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.modes = modes
        self.params = params
        self.path = path
        #: ``{"VirtualMode": "VM_ASYNC"}`` —— 库里 ``<RefParameter>`` 声明的默认值
        self.virtual_defaults = virtual_defaults or {}
        #: ``VS_ClockSourceINT`` -> 声明了它的模式名（按文件顺序）。
        #: 同一个 VS 信号常常被多个模式声明，见 `virtual_pin_mode`。
        self.vs_signals = vs_signals or {}

    @classmethod
    def load(cls, path: str | Path) -> ModeDb:
        p = Path(path)
        if not p.exists():
            raise DbError(f"模式库不存在: {p}")
        return _load_cached(str(p))

    # ---------------------------------------------------------------- 查询

    def get(self, mode: str) -> ModeDef | None:
        return self.modes.get(mode)

    def concrete_modes(self) -> tuple[ModeDef, ...]:
        """可选的模式（去掉 abstract 基类，按文件顺序）。"""
        return tuple(m for m in self.modes.values() if not m.abstract)

    def default_mode(self) -> ModeDef | None:
        """没指定模式时该用哪个。

        规则：拿库里 ``<RefParameter Name="VirtualMode" DefaultValue="VM_ASYNC">``
        声明的默认值去反查**第一个**取值相符的模式 —— USART 得到 ``Asynchronous``
        ，SPI 得到 ``Full_Duplex_Master``，都正是 CubeMX GUI 里的默认选项。
        没有虚拟键的外设（I2C/TIM）退化为「第一个声明了信号需求的模式」。

        只在 ``Device.modes()`` 给了模式库时才有意义；没有则调用方必须显式指定。
        """
        usable = [m for m in self.concrete_modes() if m.signals]
        if not usable:
            return None
        for key, value in self.virtual_defaults.items():
            for m in usable:
                if m.virtual and m.virtual[0] == key and m.virtual[1] == value:
                    return m
        return usable[0]

    def required_signals(self, mode: str) -> frozenset[str] | None:
        """该模式需要的信号；文件里没写返回 None（表示无从校验）。"""
        m = self.modes.get(mode)
        return frozenset(m.signals) if m and m.signals else None

    def virtual(self, mode: str) -> tuple[str, str] | None:
        """该模式对应的外设级模式键值对（``VirtualMode`` 或 ``VirtualType``）。

        没有这个概念时返回 None —— 那时调用方**不该写这个键**。
        """
        m = self.modes.get(mode)
        return m.virtual if m else None

    def virtual_mode(self, mode: str) -> str | None:
        """只取 ``VirtualMode`` 的值（SPI 的 ``VirtualType`` 另见 `virtual`）。"""
        m = self.modes.get(mode)
        return m.virtual_mode if m else None

    def virtual_pin_mode(self, vs_signal: str) -> str | None:
        """虚拟引脚 ``VP_<IP>_<vs_signal>`` 的 ``Mode`` 该写哪个模式名。

        **这个键不能省。** 实测：TIM3 的 ``VP_TIM3_VS_ClockSourceINT`` 少了
        ``Mode``，``config load`` → ``saveas`` 之后 **TIM3 会从 ``Mcu.IP*`` 里
        消失**（引脚还在清单里，外设却没了）—— 生成的代码不会初始化它。
        写了就保住了。

        一个 VS 信号常被多个模式声明，比如 ``VS_ClockSourceINT`` 同时属于
        ``Enable_Timer`` 和 ``Internal``。这时候选**有 ``UserName`` 的那个** ——
        没有 UserName 的是内部伪模式（不属于 GUI 里可点的选项），实测 CubeMX
        要的正是 ``Internal`` 而不是 ``Enable_Timer``。
        """
        declared = self.vs_signals.get(vs_signal)
        if not declared:
            return None
        for name in declared:
            m = self.modes.get(name)
            if m is not None and m.user_name:
                return name
        return declared[0]

    def mode_params(self, mode: str) -> frozenset[str]:
        m = self.modes.get(mode)
        return frozenset(m.params) if m else frozenset()

    def param(self, name: str) -> ParamDef | None:
        return self.params.get(name)

    def is_valid_value(self, param: str, value: str) -> bool:
        """参数值是否合法。没见过的参数、或不是枚举类型的，一律放行。"""
        return self.check_value(param, value) is None

    def check_value(self, param: str, value: str) -> str | None:
        """按库里的 ``PossibleValue`` 枚举核对取值，合法返回 None，否则返回原因
        （含最接近的写法和全部可选值）。

        放行：库里没这个参数、不是枚举、值是逗号分隔的多选（``PinAttribute``）。
        """
        d = self.params.get(param)
        # 只列了一个可选值的，多半是"在某条件下锁定为它"（实测 FMC 时序 `可选：15`、
        # RIF `可选：true`），官方示例工程里写的却是别的值 —— 不能当取值全集。
        if d is None or len(d.values) < 2:
            return None
        if value in d.values or "," in value:
            return None
        from difflib import get_close_matches

        near = get_close_matches(value, d.values, n=1, cutoff=0.4)
        return (
            f"{param}={value} 不在取值范围内。"
            + (f"你是不是想写 {near[0]}？" if near else "")
            + f"可选：{', '.join(d.values)}"
        )

    def check_range(self, param: str, value: str) -> str | None:
        """按 ``Min``/``Max`` 核对数值。**只能当提示用**：库里的范围常带条件
        （波特率上限随时钟变、下限写成 ``=#Window`` 这种表达式），本工具不求值。
        """
        d = self.params.get(param)
        if d is None or d.values:
            return None
        lo, hi, num = _num(d.minimum), _num(d.maximum), _num(value)
        if num is None or (lo is None and hi is None):
            return None
        if (lo is not None and num < lo) or (hi is not None and num > hi):
            return f"{param}={value} 超出库里的范围 [{d.minimum}, {d.maximum}]（该范围可能随配置变化）"
        return None

    def default_value(self, param: str) -> str | None:
        d = self.params.get(param)
        return d.default if d else None

    def __repr__(self) -> str:
        return f"ModeDb({len(self.modes)} modes, {len(self.params)} params)"


def _num(text: str | None) -> float | None:
    """``"128"`` / ``"0x1F"`` -> 数字；表达式（``$x*2``）或空返回 None。"""
    if text is None:
        return None
    s = text.strip()
    try:
        return float(int(s, 16)) if s.lower().startswith("0x") else float(s)
    except ValueError:
        return None


@lru_cache(maxsize=32)
def _load_cached(path: str) -> ModeDb:
    root = parse(path)

    # ---- 参数定义：同名合并（并集），条件表达式不求值
    params: dict[str, ParamDef] = {}
    virtual_defaults: dict[str, str] = {}
    #: 不带 ``<Condition>`` 的声明里的 DefaultValue —— 只有它才是真默认值（见模块说明）
    plain_defaults: dict[str, str] = {}
    for el in descendants(root, "RefParameter"):
        name = el.get("Name")
        if not name:
            continue
        if child(el, "Condition") is None and el.get("DefaultValue"):
            plain_defaults.setdefault(name, el.get("DefaultValue"))
        if name in VIRTUAL_KEYS and el.get("DefaultValue"):
            virtual_defaults.setdefault(name, el.get("DefaultValue"))
        values = tuple(
            v.get("Value")
            for v in children(el, "PossibleValue")
            if v.get("Value")
        )
        prev = params.get(name)
        if prev is None:
            params[name] = ParamDef(
                name=name,
                default=el.get("DefaultValue"),
                group=el.get("Group"),
                type=el.get("Type") or None,
                values=values,
                minimum=el.get("Min"),
                maximum=el.get("Max"),
                comment=el.get("Comment") or None,
            )
        elif values:
            merged = tuple(dict.fromkeys(prev.values + values))
            params[name] = ParamDef(
                name=name,
                default=prev.default if prev.default is not None else el.get("DefaultValue"),
                group=prev.group or el.get("Group"),
                type=prev.type or el.get("Type") or None,
                values=merged,
                minimum=prev.minimum,
                maximum=prev.maximum,
                comment=prev.comment,
            )

    # 上面按"先见先得"留下的 default 可能来自一条带条件的声明（只在某个模式下
    # 成立）。**无条件的那条覆盖回去** —— 它是任何模式下都成立的默认值。
    for name, dv in plain_defaults.items():
        d = params.get(name)
        if d is not None and d.default != dv:
            params[name] = replace(d, default=dv)

    # ---- RefMode：模式定义（参数、VirtualMode/VirtualType、继承）
    @dataclass
    class _Raw:
        base: str | None
        abstract: bool
        own: tuple[str, ...]
        vkey: str | None
        vvals: tuple[str, ...]

    raw: dict[str, _Raw] = {}
    for el in descendants(root, "RefMode"):
        name = el.get("Name")
        if not name:
            continue
        own: list[str] = []
        vkey: str | None = None
        vvals: list[str] = []
        for pa in children(el, "Parameter"):
            pname = pa.get("Name")
            if not pname:
                continue
            if pname in VIRTUAL_KEYS:
                vals = [
                    v.get("Value") or (v.text or "").strip()
                    for v in children(pa, "PossibleValue")
                ]
                vals = [v for v in vals if v]
                if vals:
                    vkey = vkey or pname
                    vvals.extend(vals)
                continue
            own.append(pname)
        abstract = (el.get("Abstract") or "").lower() == "true"
        base = el.get("BaseMode")
        prev = raw.get(name)
        if prev is None:
            raw[name] = _Raw(base, abstract, tuple(own), vkey, tuple(vvals))
        else:
            # 同名多条：取并集，保留已有的 base/abstract
            raw[name] = _Raw(
                prev.base or base,
                prev.abstract or abstract,
                tuple(dict.fromkeys(prev.own + tuple(own))),
                prev.vkey or vkey,
                tuple(dict.fromkeys(prev.vvals + tuple(vvals))),
            )

    def expand(name: str, seen: frozenset[str]) -> tuple[str, ...]:
        """沿 BaseMode 递归展开参数名。"""
        entry = raw.get(name)
        if entry is None or name in seen:
            return ()
        inherited = expand(entry.base, seen | {name}) if entry.base else ()
        return tuple(dict.fromkeys(inherited + entry.own))

    # ---- Mode：信号需求（``<Mode Name="X"><SignalLogicalOp>…``）
    signals: dict[str, tuple[str, ...]] = {}
    ops: dict[str, str] = {}
    user_names: dict[str, str] = {}
    vs_signals: dict[str, tuple[str, ...]] = {}
    for el in descendants(root, "Mode"):
        name = el.get("Name")
        if not name:
            continue
        # 有 UserName 才是 GUI 里真能选的一项（详见 ModeDef.user_name）
        if el.get("UserName"):
            user_names.setdefault(name, el.get("UserName"))
        lop = child(el, "SignalLogicalOp")  # 只看直接子元素，避免抓到嵌套的
        if lop is None:
            continue
        found = tuple(
            s.get("Name") for s in children(lop, "Signal") if s.get("Name")
        )
        if not found:
            continue
        signals[name] = tuple(dict.fromkeys(signals.get(name, ()) + found))
        ops[name] = lop.get("Name") or "AND"
        for s in found:
            if s.startswith("VS_"):
                prev = vs_signals.get(s, ())
                if name not in prev:
                    vs_signals[s] = prev + (name,)

    empty = _Raw(None, False, (), None, ())
    modes: dict[str, ModeDef] = {}
    for name in dict.fromkeys(list(raw) + list(signals)):
        r = raw.get(name, empty)
        modes[name] = ModeDef(
            name=name,
            abstract=r.abstract,
            base=r.base,
            user_name=user_names.get(name),
            virtual_key=r.vkey,
            virtual_values=r.vvals,
            signals=signals.get(name),
            signal_op=ops.get(name, "AND"),
            params=expand(name, frozenset()),
        )

    if not modes:
        raise DbError(f"{path}: 没解析出任何模式，格式可能变了")
    return ModeDb(modes, params, Path(path), virtual_defaults, vs_signals)


__all__ = ["DbError", "ModeDb", "ModeDef", "ParamDef"]
