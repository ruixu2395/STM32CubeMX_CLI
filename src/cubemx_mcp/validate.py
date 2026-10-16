"""工程校验。

**这一层是唯一的防线。** CubeMX 对 ``config load`` → ``saveas`` 不做任何一致性
检查（实测：引脚有 ``Signal`` 而外设不在 ``Mcu.IP*``、``Mode`` 缺失的工程会被
静默接受，生成的代码里那个外设根本不会初始化）。所以"配置准不准"只能靠这里。

分五层，由强到弱：

1. **引脚能力** —— 该脚在这颗 MCU 上是否真的能承载这个 signal。
   （``PA11`` 有 ``USART1_CTS`` 却没有 ``USART1_RX``，光看 signal 名看不出来。）
2. **归属与登记** —— ``<PIN>.Signal`` 推出的外设是否登记进了 ``Mcu.IP*``；
   反向也要查：``Mcu.IP*`` 里的外设是不是真的有引脚，否则是个空壳。
3. **模式自洽** —— 引脚 ``Mode`` 是否在模式库里；该模式要求的信号是否都给齐了
   （``Synchronous`` 要 CK+RX+TX，只给 TX/RX 是错的）；外设级模式键
   （``VirtualMode`` / ``VirtualType``）是否与引脚 ``Mode`` 对应。
4. **参数合法性** —— ``<IP>.IPParameters`` 是否登记了每个用到的参数；参数名是否
   属于该模式；枚举值是否在取值范围内；有没有名字打错的（附相近建议）。
5. **跨外设状态** —— 中断/DMA 挂了没配好的外设（``irq.orphan`` / ``dma.orphan``）；
   写了 CubeMX 会静默丢弃的键（``nvic.bogus_key``）。这一层只发 **warning、不挡落盘**
   —— 它说的是"现在还没坏、CubeMX 一保存就坏"。

每层单独一个 `check_*` 函数，都返回 `Issue` 列表，`validate()` 汇总。
**没有数据库（``ST_CUBEMX_DB`` 未设置）时第 1、3、4 层降级为"跳过"**，并在
报告里说明跳过了什么 —— 不能因为查不到就谎报"通过"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches

from .db import Device
from .ioc import signals as sig
from .ioc.layout import SYSTEM_IPS
from .ioc.project import (
    PIN_STRUCTURAL_KEYS,
    VIRTUAL_KEYS,
    IocProject,
    is_virtual_pin,
)

#: 免于"必须有引脚"检查的外设：CubeMX 无条件登记、不接引脚的系统级伪外设。
#: 另有 ``SYS`` —— 它靠 ``VP_SYS_VS_*`` 虚拟引脚存在，也在下面单独放行。
_PINLESS_OK = SYSTEM_IPS | {"SYS", "GPIO"}


@dataclass(frozen=True)
class Issue:
    """一条校验发现。"""

    level: str  # error / warning / info
    code: str  # 稳定的机器可读标识
    message: str
    subject: str | None = None  # 出问题的引脚名或外设名
    hint: str | None = None

    def __str__(self) -> str:
        # 带上 code：纯文本输出里也能一眼看出是哪类问题，脚本/AI 才好按类别处理
        head = f"[{self.level}] {self.code}"
        if self.subject:
            head += f" {self.subject}"
        tail = f"\n    建议：{self.hint}" if self.hint else ""
        return f"{head}: {self.message}{tail}"

    def to_dict(self) -> dict:
        """给 MCP / `--json` 用的结构化形态。**逐字段写死**，不用 ``__dict__`` ——
        后者会把将来新增的内部字段也漏出去，变成事实上的公开接口。"""
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "subject": self.subject,
            "hint": self.hint,
        }


@dataclass
class Report:
    """一次校验的全部结果。"""

    issues: list[Issue] = field(default_factory=list)
    #: 因为缺数据库而没做的检查
    skipped: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        """有没有 error（warning 不影响落盘）。"""
        return not self.errors

    def add(self, *issues: Issue) -> None:
        self.issues.extend(issues)

    def to_dict(self) -> dict:
        """结构化形态。字段与 `cubemx-cli validate --json` 的输出**完全一致** ——
        CLI 和 MCP 共用这一份，别让两条路各写一个形状。"""
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "skipped": list(self.skipped),
        }

    def render(self) -> str:
        if not self.issues and not self.skipped:
            return "校验通过：没有发现问题。"
        lines: list[str] = []
        for i in self.issues:
            lines.append(str(i))
        if self.skipped:
            lines.append("跳过的检查：" + "；".join(self.skipped))
        if self.issues:
            lines.append(
                f"合计 {len(self.errors)} 个错误、{len(self.warnings)} 个警告。"
            )
        return "\n".join(lines)


# ------------------------------------------------------------------ 第 1 层


def check_pin_capability(project: IocProject) -> tuple[list[Issue], str | None]:
    """每个引脚的 signal 是否真的能落在这个脚上。"""
    device = project.device
    if device is None:
        return [], "引脚能力（缺 CubeMX 数据库）"

    issues: list[Issue] = []
    for pin in project.pins():
        signal = pin.signal
        if not signal:
            continue

        if is_virtual_pin(pin.name):
            continue  # VP_* 是 CubeMX 内部引脚，不在 MCU 引脚表里

        cap = device.mcu.pins.get(pin.name)
        if cap is None:
            near = get_close_matches(pin.name, list(device.mcu.pins), n=3, cutoff=0.6)
            issues.append(
                Issue(
                    "error",
                    "pin.unknown",
                    f"{pin.name} 不在这颗 MCU（{device.mcu.name}）的引脚表里",
                    subject=pin.name,
                    hint=f"相近的引脚：{', '.join(near)}" if near else None,
                )
            )
            continue

        if cap.can(signal):
            continue

        # 给出可用的替代引脚，比单纯说"不行"有用得多
        if sig.is_gpio_signal(signal):
            hint = f"{pin.name} 支持的 GPIO 模式：{', '.join(sorted(cap.gpio_modes)) or '无'}"
        else:
            owner = device.mcu.signal_owner(signal)
            short = signal[len(owner) + 1:] if owner and signal.startswith(owner + "_") else signal
            opts = device.mcu.pins_for(owner, short) if owner else ()
            hint = f"该信号可用的引脚：{', '.join(opts)}" if opts else "这颗 MCU 上没有任何引脚支持它"
        issues.append(
            Issue(
                "error",
                "pin.unsupported_signal",
                f"{pin.name} 不能承载 {signal}",
                subject=pin.name,
                hint=hint,
            )
        )
    return issues, None


# ------------------------------------------------------------------ 第 2 层


def check_registration(project: IocProject) -> list[Issue]:
    """引脚归属的外设是否登记；登记的外设是否真的有引脚。"""
    issues: list[Issue] = []
    ips = set(project.peripheral_names())

    # 正向：引脚指向的外设必须在 Mcu.IP* 里，否则 CubeMX 不会初始化它
    for pin in project.pins():
        owner = project.owner_of(pin)
        if owner is None or is_virtual_pin(pin.name):
            continue
        if owner not in ips:
            issues.append(
                Issue(
                    "error",
                    "ip.not_registered",
                    f"{pin.name} 用了 {pin.signal}，但外设 {owner} 不在 Mcu.IP* 里，"
                    "生成的代码不会初始化它",
                    subject=owner,
                    hint=f"登记它：cubemx-cli add-peripheral <工程> {owner}",
                )
            )

    # 反向：登记了却没有引脚的外设是个空壳
    for name in project.peripheral_names():
        if name in _PINLESS_OK:
            continue
        users = [
            p for p in project.pins()
            if project.owner_of(p) == name
        ]
        if users:
            continue
        per = project.find_peripheral(name)
        # 只设了参数、没有任何引脚的中间状态：警告而非错误，可能是用户先配参数
        issues.append(
            Issue(
                "warning",
                "ip.no_pins",
                f"外设 {name} 登记在 Mcu.IP* 里但没有任何引脚"
                + ("（只有参数）" if per and per.parameters else ""),
                subject=name,
                hint=f"删掉它：cubemx-cli remove-peripheral <工程> {name}",
            )
        )
    return issues


# ------------------------------------------------------------------ 第 3 层


def check_modes(project: IocProject) -> tuple[list[Issue], str | None]:
    """引脚 Mode、必需信号、外设级模式键三者是否自洽。"""
    device = project.device
    if device is None:
        return [], "模式自洽（缺 CubeMX 数据库）"

    issues: list[Issue] = []

    # 按外设分组，才能判"信号齐不齐"和"外设级键对不对"
    groups: dict[str, list] = {}
    for pin in project.pins():
        owner = project.owner_of(pin)
        if owner is None or is_virtual_pin(pin.name):
            continue
        groups.setdefault(owner, []).append(pin)

    for owner, pins in sorted(groups.items()):
        mdb = device.modes(owner)
        if mdb is None:
            ref = device.mcu.ip(owner)
            name = ref.modes_file_name if ref else "?"
            issues.append(
                Issue(
                    "info",
                    "modes.unavailable",
                    f"数据库里没有 {owner} 的模式库（{name}），跳过模式校验",
                    subject=owner,
                )
            )
            continue

        # --- 引脚 Mode 是否已知
        modes = {p.mode for p in pins if p.mode}
        for pin in pins:
            if pin.mode and mdb.get(pin.mode) is None:
                near = get_close_matches(pin.mode, list(mdb.modes), n=3, cutoff=0.6)
                issues.append(
                    Issue(
                        "error",
                        "mode.unknown",
                        f"{pin.name}.Mode={pin.mode} 不在 {owner} 的模式库里",
                        subject=pin.name,
                        hint=f"相近的模式：{', '.join(near)}" if near else None,
                    )
                )

        # --- 非 GPIO 信号必须有 Mode（GPIO 引脚按 CubeMX 惯例不写 Mode）
        for pin in pins:
            if pin.mode or sig.is_gpio_signal(pin.signal or ""):
                continue
            issues.append(
                Issue(
                    "error",
                    "mode.missing",
                    f"{pin.name} 有 Signal={pin.signal} 却没有 Mode；"
                    "CubeMX 不会把这一脚算进外设初始化",
                    subject=pin.name,
                    hint=f"补上：cubemx-cli set-pin <工程> {pin.name} --mode <模式>",
                )
            )

        # --- 同一外设的引脚 Mode 应当一致（TX 是 Asynchronous、RX 是别的就矛盾）
        if len(modes) > 1:
            issues.append(
                Issue(
                    "error",
                    "mode.inconsistent",
                    f"{owner} 的引脚用了多个模式：{', '.join(sorted(modes))}",
                    subject=owner,
                    hint=f"统一成一个：cubemx-cli add-peripheral <工程> {owner} --mode <模式> --force",
                )
            )

        mode = next(iter(modes), None)
        if mode is None:
            continue
        mdef = mdb.get(mode)
        if mdef is None or not mdef.signals:
            continue

        # --- 必需信号是否给齐
        have = {(p.signal or "")[len(owner) + 1:] for p in pins}
        have.discard("")
        if mdef.signal_op == "AND":
            missing = [s for s in mdef.signals if s not in have]
            if missing:
                issues.append(
                    Issue(
                        "error",
                        "mode.missing_signals",
                        f"{owner} 的模式 {mode} 需要 {', '.join(mdef.signals)}，缺 {', '.join(missing)}",
                        subject=owner,
                        hint=f"补齐：cubemx-cli add-peripheral <工程> {owner} --mode {mode}",
                    )
                )
        elif not (set(mdef.signals) & have):
            # OR：任选一个即可。嵌套的 SignalLogicalOp 分组在 db 层被拍平了，
            # 所以这里只能判"一个都没给"这种最明显的情况，不做分组推断。
            issues.append(
                Issue(
                    "error",
                    "mode.missing_signals",
                    f"{owner} 的模式 {mode} 需要 {', '.join(mdef.signals)} 中至少一个，一个都没给",
                    subject=owner,
                )
            )

        # --- 外设级模式键与引脚 Mode 是否对应
        per = project.find_peripheral(owner)
        if per is None:
            continue
        want = mdef.virtual
        got = per.virtual
        if want is None and got is not None:
            issues.append(
                Issue(
                    "warning",
                    "virtual.unexpected",
                    f"{owner} 写了 {got[0]}={got[1]}，但模式 {mode} 没有这个概念",
                    subject=owner,
                    hint=f"删掉它：cubemx-cli set-peripheral <工程> {owner} --unset {got[0]}",
                )
            )
        elif want is not None and got is None:
            issues.append(
                Issue(
                    "error",
                    "virtual.missing",
                    f"{owner} 的模式是 {mode}，必须同时写 {want[0]}={want[1]}，现在没有",
                    subject=owner,
                    hint=f"补上：cubemx-cli add-peripheral <工程> {owner} --mode {mode}",
                )
            )
        elif want is not None and got is not None and got != want:
            issues.append(
                Issue(
                    "error",
                    "virtual.mismatch",
                    f"{owner} 引脚 Mode={mode} 对应 {want[0]}={want[1]}，"
                    f"但工程里写的是 {got[0]}={got[1]}",
                    subject=owner,
                    hint=f"改成 {want[0]}={want[1]}",
                )
            )
    return issues, None


# ------------------------------------------------------------------ 第 4 层


def check_parameters(project: IocProject) -> tuple[list[Issue], str | None]:
    """参数登记、名字、取值。"""
    issues: list[Issue] = []
    device = project.device

    for per in project.peripherals():
        name = per.name
        # NVIC / RCC 恒在 Mcu.IP* 里，但它们的键（NVIC.xxx / RCC.xxx）走自己的
        # 命名空间，**不用 IPParameters 登记**，套第 4 层会全量误报。
        if name in SYSTEM_IPS:
            continue
        registered = set(per.parameters)
        # 用 values（含 VirtualMode 这类顶层字段）而不是 properties：核对登记表
        # 时漏掉顶层字段会把"已登记且有值"误报成"登记了却没值"。
        props = per.values

        # --- 设了键却没登记进 IPParameters：CubeMX 打开工程时静默丢掉
        for key in sorted(props):
            if key not in registered:
                issues.append(
                    Issue(
                        "error",
                        "param.unregistered",
                        f"{name}.{key} 没有登记进 {name}.IPParameters，"
                        "CubeMX 加载时会把这一行丢掉",
                        subject=name,
                        hint=f"重设一次即可自动登记：cubemx-cli set-peripheral <工程> {name} "
                             f"--param {key}={props[key]}",
                    )
                )

        # --- 登记了却没有对应的键：反向的半配置
        for key in sorted(registered):
            if key not in props:
                issues.append(
                    Issue(
                        "warning",
                        "param.dangling",
                        f"{name}.IPParameters 登记了 {key}，但 {name}.{key} 没有值",
                        subject=name,
                    )
                )

        if device is None:
            continue

        mdb = device.modes(name)
        if mdb is None:
            continue

        # 该外设当前生效的模式，决定哪些参数名合法
        pins = [p for p in project.pins() if project.owner_of(p) == name and p.mode]
        mode = pins[0].mode if pins else None
        allowed = mdb.mode_params(mode) if mode else frozenset()

        for key, value in sorted(props.items()):
            # 外设级模式键由第 3 层单独管，不在模式参数表里，别在这里重复报
            if key in ("VirtualMode", "VirtualType"):
                continue
            if not allowed or key in allowed:
                continue
            near = get_close_matches(key, sorted(allowed), n=1, cutoff=0.5)
            issues.append(
                Issue(
                    "error",
                    "param.not_in_mode",
                    f"{name}.{key} 不属于模式 {mode} 的参数",
                    subject=name,
                    hint=f"你是不是想写 {near[0]}？" if near
                    else f"{mode} 允许：{', '.join(sorted(allowed))}",
                )
            )

        for key, value in sorted(props.items()):
            if key in ("VirtualMode", "VirtualType"):
                continue
            if mdb.is_valid_value(key, value):
                continue
            d = mdb.param(key)
            issues.append(
                Issue(
                    "error",
                    "param.bad_value",
                    f"{name}.{key}={value} 不在取值范围内",
                    subject=name,
                    hint=f"可选：{', '.join(d.values)}" if d else None,
                )
            )
    # 登记表那两条检查不需要数据库，上面已经跑完；只有"参数名属于哪个模式"和
    # "取值是否合法"要查模式库。不能说成"全层通过"。
    return issues, ("参数名与取值（缺 CubeMX 数据库）" if device is None else None)


# ------------------------------------------------------------------ 引脚参数


def check_gpio_parameters(project: IocProject) -> list[Issue]:
    """``<PIN>.GPIOParameters`` 登记表与 ``<PIN>.GPIO_*`` 键是否对得上。

    和 ``IPParameters`` 是同一套机制：没登记的参数键会被 CubeMX 静默丢弃。
    ST 官方的板级文件里每个带 GPIO 参数的引脚都写了这一行。
    """
    issues: list[Issue] = []
    for pin in project.pins():
        if is_virtual_pin(pin.name):
            continue  # VP_* 由 CubeMX 自管，登记表不按这套规则
        registered = set(pin.parameters)
        props = pin.values
        for key in sorted(props):
            if key not in registered and key not in PIN_STRUCTURAL_KEYS:
                issues.append(
                    Issue(
                        "error",
                        "gpio.param_unregistered",
                        f"{pin.name}.{key} 没有登记进 {pin.name}.GPIOParameters，"
                        "CubeMX 加载时会把这一行丢掉",
                        subject=pin.name,
                        hint=f"重设一次即可自动登记：cubemx-cli set-pin <工程> {pin.name} "
                             f"--set {key}={props[key]}",
                    )
                )
        for key in sorted(registered):
            if key not in props:
                issues.append(
                    Issue(
                        "warning",
                        "gpio.param_dangling",
                        f"{pin.name}.GPIOParameters 登记了 {key}，但 {pin.name}.{key} 没有值",
                        subject=pin.name,
                    )
                )
    return issues


def check_duplicate_signals(project: IocProject) -> list[Issue]:
    """同一个 signal 被两个引脚占用 —— CubeMX 会取其中一个，结果不可预期。

    **``GPIO_Output`` / ``GPIO_Input`` / ``GPXTI<n>`` 这类不算**：它们是引脚的
    工作模式而非外设功能，本来就有几十个引脚同时是 ``GPIO_Output``。把它们也算
    进来会满屏误报。
    """
    seen: dict[str, list[str]] = {}
    for pin in project.pins():
        signal = pin.signal
        if signal and not sig.is_gpio_signal(signal):
            seen.setdefault(signal, []).append(pin.name)
    return [
        Issue(
            "error",
            "signal.duplicated",
            f"{signal} 被多个引脚占用：{', '.join(pins)}",
            subject=signal,
            hint=f"保留一个：cubemx-cli clear-pin <工程> {pins[1]}",
        )
        for signal, pins in sorted(seen.items())
        if len(pins) > 1
    ]


# ------------------------------------------------------- 第 5 层：NVIC 与 DMA


def check_nvic_keys(project: IocProject) -> list[Issue]:
    """``NVIC.`` 前缀下**不存在**的那个键 —— ``NVIC.IParameters``。

    这一条精确到只有一个键名，刻意的。``NVIC.*`` 的键名空间是 CubeMX 私有的
    （``NVIC.<IRQn>`` / ``NVIC.PriorityGroup`` / ``NVIC.ForceEnableDMAVector``，
    还有我们没见过的），**做通用白名单一定会在没见过的 MCU 上误报**。
    而 ``NVIC.IParameters`` 是唯一一个能被证伪的：它是别的键名空间的习惯写法
    （``<IP>.IPParameters`` / ``<PIN>.GPIOParameters`` 两张登记表），
    实测 **524 份 ST 官方板级文件里出现 0 次** —— 写了它 CubeMX 加载时静默丢弃。
    """
    if "NVIC.IParameters" not in project.doc:
        return []
    return [
        Issue(
            "warning",
            "nvic.bogus_key",
            "NVIC.IParameters 不是 NVIC 的登记表，CubeMX 保存时会静默丢掉这一行",
            subject="NVIC",
            hint="删掉它：cubemx-cli set-peripheral <工程> NVIC --unset IParameters"
            "（登记表只存在于 <外设>.IPParameters 和 <引脚>.GPIOParameters 上）",
        )
    ]


def check_irq_dma_orphans(project: IocProject) -> list[Issue]:
    """为**配置不完整的外设**开了中断 / 挂了 DMA。

    这两种都**不报错**，是这份格式最危险的地方：

    * **中断会被悄悄删掉。** 实测：外设登记在
      ``Mcu.IP*`` 里、``VirtualMode`` 也写了，但一个引脚都没配 —— 往返之后
      外设整个从 ``Mcu.IP*`` 消失，``NVIC.<IRQn>`` **那一行也跟着没了**。
    * **DMA 块会留成"死块"。** 同一次实测里 ``Dma.*`` 反而**原样存活**，看起来
      完全正常，而那个请求永远不会触发。往返验证抓不到这一种。

    所以两条都是 warning、都不挡落盘：它们描述的是"现在还没坏、CubeMX 一保存就坏"，
    和 ``INCOMPLETE_CODES`` 那条"还没配完"是同一个精神 —— 提醒，不拦。

    **判据只取"登记了却一个引脚都没占"这一种，不用"不在 ``Mcu.IP*`` 里"。**
    后者听着更宽，实测却会误报：17 份 ST 官方板级文件带 ``NVIC.EXTI0_IRQn``，
    **没有一份**把 ``EXTI0`` 写进 ``Mcu.IP*`` —— EXTI 线靠引脚的 ``GPXTI<n>``
    表达，不登记。那种组合既是 CubeMX 自己写出来的，就一定是它认的。
    """
    ips = set(project.peripheral_names())
    pins = project.pins()
    device = project.device

    def _pinless(name: str) -> bool:
        """登记在 ``Mcu.IP*`` 里、**声明了模式**、却没有任何引脚指向它。

        三个条件缺一不可，前两个都是实测撞出来的：

        * ``DMA`` / ``RCC`` 这类控制器和系统块库里根本没有信号，``Mcu.IP*=DMA``
          是 CubeMX 自己的写法。拿"没有引脚"去说它们，**每一份正常工程**都会挨一记。
          所以要求库里**有**以这个实例名打头的信号 —— 那才说明它是个该带引脚的实例。
          （代价要如实说：信号名不带实例名前缀的外设，比如信号叫裸 ``CEC`` 的
          ``HDMI_CEC``、信号叫 ``S_DATAIN5DFSDM1`` 的 ``DFSDM1``，这里一律不说。）
        * **必须有 ``VirtualMode`` / ``VirtualType``。** 没有它，"没引脚"根本不是
          问题：ADC 走内部通道（``Channel-0#ChannelRegularConversion``）、DFSDM 走
          滤波器参数、TIM 走强制输出，都是**合法的无引脚配置**。实测扫 524 份 ST
          官方板级文件：触发这条的 5 份**全都没有**模式键，全是误报；而
          那种真会出事的外设（``USART1.VirtualMode=VM_ASYNC``，登记了却没引脚）
          有。模式键在这里是对的判据而不只是巧合 —— **是模式在要求引脚**。

        代价是"信号名不带实例名前缀 + 又声明了模式"那一小撮漏报。漏报比误报好：
        一条假的 warning 会让人去查一个不存在的故障，而这正是这份文件最该避免的。
        """
        if name not in ips or name in _PINLESS_OK or device is None:
            return False
        if any(project.owner_of(p) == name for p in pins):
            return False
        per = project.find_peripheral(name)
        if per is None or not any(per.get_param(k) for k in VIRTUAL_KEYS):
            return False
        prefix = (name + "_", name + "-")
        return any(
            sig == name or sig.startswith(prefix)
            for cap in device.mcu.ordered_pins()
            for sig in cap.signals
        )

    issues: list[Issue] = []

    if device is not None:
        irq_db = device.irqs()
        if irq_db is not None:
            for key in sorted(project.doc.keys_with_prefix("NVIC.")):
                name = key[len("NVIC."):]
                if "." in name or not name.endswith("_IRQn"):
                    continue
                irq = irq_db.get(name)
                # owners 为空的（SysTick / HardFault 那些系统向量）**不说** ——
                # 没有归属信息就不该编一个出来。
                if irq is None or not irq.owners:
                    continue
                owners = [o for o in irq.owners if _pinless(o)]
                if not owners:
                    continue
                issues.append(
                    Issue(
                        "warning",
                        "irq.orphan",
                        f"{name} 开给 {owners[0]}，但 {owners[0]} 一个引脚都没配 —— "
                        "CubeMX 保存时会把外设和这一行一起删掉",
                        subject=name,
                        hint=f"先把 {owners[0]} 配好，或关掉它："
                             f"cubemx-cli disable-irq <工程> {name}",
                    )
                )

        dma_db = device.dma()
        if dma_db is not None:
            for key in sorted(project.doc.keys_with_prefix("Dma.Request")):
                raw_idx = key[len("Dma.Request"):]
                request = project.doc.get(key) if raw_idx.isdigit() else None
                if not request:
                    continue
                req = dma_db.request(request)
                if req is None or not req.owners:
                    continue
                owners = [o for o in req.owners if _pinless(o)]
                if not owners:
                    continue
                issues.append(
                    Issue(
                        "warning",
                        "dma.orphan",
                        f"DMA 请求 {request} 挂给了 {owners[0]}，但 {owners[0]} "
                        "一个引脚都没配 —— CubeMX 会保住这个 DMA 块，而它永远不会触发",
                        subject=request,
                        hint=f"先把 {owners[0]} 配好，或摘掉它："
                             f"cubemx-cli remove-dma <工程> {request}",
                    )
                )
    return issues


# ------------------------------------------------------------------ 汇总


#: 这些 error 描述的是**"还没配完"**，而不是"配错了"：
#:
#: * ``mode.missing`` —— 引脚写了 ``Signal`` 还没写 ``Mode``
#: * ``mode.missing_signals`` —— 外设的信号还没给齐（``USART2`` 只配了 TX）
#: * ``virtual.missing`` —— 引脚 ``Mode`` 定了，外设级模式键还没写
#:
#: 逐字段命令（``set-pin`` / ``set-peripheral``）天然要路过这些中间状态 ——
#: 用户自己的流程就是先 ``set-pin`` TX、再 ``set-pin`` RX、最后
#: ``set-peripheral --virtual-mode``。拿它们挡住落盘，逐字段命令就一步都走不了。
#:
#: 意图级命令（``add-peripheral``）一次产出自洽配置，永远不留下这些状态；所以
#: 放行它们不会削弱"工具不写自己知道是坏的工程"这条保证 —— 真正配错的键
#: （引脚扛不住这个 signal、模式名不存在、参数越界、两个模式键打架）照样挡。
INCOMPLETE_CODES = frozenset({
    "mode.missing",
    "mode.missing_signals",
    "virtual.missing",
})


def blocking_errors(report: Report) -> list[Issue]:
    """会挡住落盘的那些 error（见 `INCOMPLETE_CODES`）。"""
    return [i for i in report.errors if i.code not in INCOMPLETE_CODES]


def validate(project: IocProject, *, device: Device | None = None) -> Report:
    """跑完全部校验。``project.device`` 为空时用 ``device`` 兜底。"""
    if device is not None and project.device is None:
        project.device = device

    report = Report()
    for fn in (check_pin_capability, check_modes, check_parameters):
        found, skipped = fn(project)
        report.add(*found)
        if skipped:
            report.skipped.append(skipped)

    report.add(*check_registration(project))
    report.add(*check_gpio_parameters(project))
    report.add(*check_duplicate_signals(project))
    report.add(*check_nvic_keys(project))
    report.add(*check_irq_dma_orphans(project))
    return report


__all__ = [
    "INCOMPLETE_CODES",
    "Issue",
    "Report",
    "blocking_errors",
    "check_duplicate_signals",
    "check_gpio_parameters",
    "check_irq_dma_orphans",
    "check_modes",
    "check_nvic_keys",
    "check_parameters",
    "check_pin_capability",
    "check_registration",
    "validate",
]
