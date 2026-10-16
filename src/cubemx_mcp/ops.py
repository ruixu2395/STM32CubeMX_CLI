"""对工程的编辑操作。

分两档：

* **逐字段**（`set_pin` / `set_peripheral` / `clear_pin` …）—— 改一个字段，
  调用方自己负责前后一致。
* **意图级**（`add_peripheral` / `add_gpio`）—— 调用方只说"我要 USART2 跑
  Asynchronous"，本模块负责：查模式库得到需要哪些信号（RX+TX）、挑引脚、
  写 `Signal`、写 `Mode`、写外设级模式键（`VirtualMode`/`VirtualType`）、
  登记 `Mcu.IP*`、把参数并进 `IPParameters`。**一次调用产出一份自洽的配置。**

所有操作都只改内存，不碰磁盘。校验由 `cubemx_mcp.validate` 负责，落盘由
`cubemx_mcp.ioc.project.IocProject.save` 负责 —— 于是"校验不过就绝不落盘"
是自然结果，不需要在每个操作里做防御。

需要权威数据（引脚能不能承载某个 signal、模式需要哪些信号）时走
`cubemx_mcp.db`。没有数据库也能用逐字段操作，但 `add_*` 会拒绝执行 ——
宁可报错，也不要凭启发式瞎猜一个引脚出来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import get_close_matches

from .db import DmaDb, Device, IrqDb
from .ioc import signals as sig
from .ioc.project import IocProject, Pin, ProjectError, is_virtual_pin

#: 虚拟信号前缀。``VS_ClockSourceINT`` 这类不占真实引脚，对应一个
#: ``VP_<IP>_<Signal>`` 虚拟引脚项。
VIRTUAL_SIGNAL_PREFIX = "VS_"

#: 自动分配时，优先挑的引脚端口（CubeMX 自己也偏爱 GPIOA/GPIOB）。
#: 这里不做偏好，纯粹按封装位号顺序，好处是结果稳定可复现。


class OpError(ValueError):
    """操作无法完成（引脚冲突、外设不存在、数据库约束不满足等）。"""


# --------------------------------------------------------------------- 工具


def resolve_pin(project: IocProject, name: str) -> str:
    """把用户给的引脚名补全成数据库里的正式名。

    ``PA0`` 在库里叫 ``PA0-WKUP``，``PC13`` 叫 ``PC13-ANTI_TAMP``。用户报裸名
    时按「同端口同脚号」唯一匹配补全；匹配不到或有歧义就原样返回。
    实现在 `IocProject.resolve_pin` —— 登记的引脚必须和视图查的名字用同一套
    规整，否则会写出 CubeMX 不认识的脚名。
    """
    return project.resolve_pin(name)


def owner_of(project: IocProject, pin: Pin) -> str | None:
    """引脚当前归属的外设实例名。"""
    return project.owner_of(pin)


def pins_of(project: IocProject, ip: str) -> tuple[Pin, ...]:
    """引用了某外设的全部引脚。"""
    return tuple(p for p in project.pins() if owner_of(project, p) == ip)


def _near_ip_hint(device: Device, key: str, limit: int = 6) -> str:
    """外设名打错时的"相近的有：…"，没有候选就返回空串。

    两档：先按 3 字符前缀（``USART9`` -> ``USART1..6``），前缀对不上再按编辑距离
    （``USRT1`` -> ``USART1``）。补第二档是因为**最常见的错法是错字和字母换位**，
    而它们在 3 字符前缀下全军覆没 —— 提示恰好在该出现的时候不出现。
    """
    near = [i for i in device.mcu.ips if i.startswith(key[:3])]
    if not near:
        near = get_close_matches(key, device.mcu.ips, n=limit, cutoff=0.6)
    return f"，相近的有：{', '.join(near[:limit])}" if near else ""


def _required_device(project: IocProject, what: str) -> Device:
    if project.device is None:
        raise OpError(
            f"{what}需要 CubeMX 数据库来确认引脚能力。"
            r"设置 ST_CUBEMX_DB 指向 <STM32CubeMX>\db\mcu，"
            "或改用逐字段命令（set-pin / set-peripheral）。"
        )
    return project.device


def _busy(project: IocProject) -> dict[str, str]:
    """已被占用的引脚 -> 它的 signal。"""
    out: dict[str, str] = {}
    for pin in project.pins():
        if pin.signal:
            out[pin.name] = pin.signal
    return out


def _is_free(project: IocProject, name: str, busy: dict[str, str]) -> bool:
    """引脚是否可分配。

    「在清单里但没配置」也算占用 —— 用户把它列出来多半是有意保留的，
    自动分配不该悄无声息地占掉。
    """
    if name in busy:
        return False
    return not project.has_pin(name)


# --------------------------------------------------------------- 引脚：逐字段


def set_pin(
    project: IocProject,
    name: str,
    *,
    signal: str | None = None,
    mode: str | None = None,
    label: str | None = None,
    locked: bool | None = None,
    props: dict[str, str] | None = None,
    force: bool = False,
) -> list[str]:
    """配置单个引脚（逐字段）。

    只把**真的改了值**的字段记进返回值：``--lock`` 一个本来就锁着的脚不该报成
    "改动"，否则 CLI 会白写一次盘、白留一份备份，还谎报有变动。
    """
    pin_name = resolve_pin(project, name)
    _check_gpio_values(project, props)
    changes: list[str] = []
    pin = project.find_pin(pin_name)
    if pin is None:
        if signal is None and mode is None and not label:
            raise OpError(f"工程里没有引脚 {pin_name}，也没有给出要配置什么")
        pin = project.add_pin(pin_name)
        changes.append(f"新增引脚 {pin_name}")

    if signal is not None:
        signal = signal.strip()
        old_owner = owner_of(project, pin)
        new_owner = _owner_for_signal(project, signal)
        if old_owner and new_owner and old_owner != new_owner and not force:
            raise OpError(
                f"{pin_name} 当前属于 {old_owner}（{pin.signal}），"
                f"要改成 {new_owner} 请加 --force"
            )
        if pin.signal != signal:
            pin.signal = signal
            changes.append(f"{pin_name}.Signal = {signal}")

    if mode is not None and pin.mode != mode:
        pin.mode = mode
        changes.append(f"{pin_name}.Mode = {mode}")

    if label is not None and pin.label != (label or None):
        pin.label = label
        changes.append(f"{pin_name}.GPIO_Label = {label}")

    if locked is not None and pin.locked != locked:
        pin.locked = locked
        changes.append(f"{pin_name}.Locked = {'true' if locked else 'false'}")

    for key, value in (props or {}).items():
        if pin.get_property(key) == value:
            continue
        pin.set_property(key, value)
        changes.append(f"{pin_name}.{key} = {value}")

    # 归属的外设要登记进 Mcu.IP*，否则 CubeMX 不会初始化它
    if pin.signal:
        owner = owner_of(project, pin)
        if owner and project.find_peripheral(owner) is None:
            project.add_peripheral(owner)
            changes.append(f"登记外设 {owner}")
    return changes


def _check_gpio_values(project: IocProject, props: dict[str, str] | None) -> None:
    """引脚参数取值按 GPIO 模式库（``GPIO-*_Modes.xml``）核对，写之前就拒。

    ``GPIO_UP`` 这种错值 CubeMX 不报错，而是**静默丢掉整块配置**（实测 EXTI 边沿
    被重置、中断被删），事后极难排查 —— 所以这里是硬拦，并直接给出可选值。
    """
    if not props or project.device is None:
        return
    gpio_db = project.device.modes("GPIO")
    if gpio_db is None:
        return
    problems = [p for k, v in props.items() if (p := gpio_db.check_value(k, v))]
    if problems:
        raise OpError("；".join(problems))


def _owner_for_signal(project: IocProject, signal: str) -> str | None:
    if project.device is not None:
        return project.device.mcu.signal_owner(signal)
    return sig.signal_to_ip(signal, frozenset(project.peripheral_names()))


def clear_pin(project: IocProject, name: str) -> list[str]:
    """清掉一个引脚及其孤儿外设。"""
    pin_name = resolve_pin(project, name)
    pin = project.find_pin(pin_name)
    if pin is None:
        raise OpError(f"工程里没有引脚 {pin_name}")

    owner = owner_of(project, pin)
    project.remove_pin(pin_name)
    changes = [f"移除引脚 {pin_name}"]

    # 外设一个引脚都不剩了就是个半配置状态：留在 Mcu.IP* 里会让 CubeMX
    # 初始化一个没有引脚的 USART1。一并摘掉，并把连带丢掉的键说清楚。
    if owner is not None and not pins_of(project, owner):
        per = project.find_peripheral(owner)
        if per is not None:
            dropped = sorted(per.properties) + list(per.parameters)
            project.remove_peripheral(owner)
            changes.append(f"移除已无引脚的孤儿外设 {owner}")
            if dropped:
                changes.append(f"  连带丢弃 {owner} 的键：{', '.join(dropped)}")
    return changes


# ----------------------------------------------------------------- 外设：逐字段


def set_peripheral(
    project: IocProject,
    ip: str,
    *,
    virtual: tuple[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> list[str]:
    """配置外设的参数（逐字段）。``virtual`` 形如 ``("VirtualMode", "VM_ASYNC")``。"""
    key = ip.strip()
    if not project.find_peripheral(key):
        project.add_peripheral(key)
        changes = [f"新增外设 {key}"]
    else:
        changes = []

    per = project.find_peripheral(key)
    assert per is not None

    mdb = project.device.modes(key) if project.device is not None else None
    if mdb is not None:
        bad = [p for k, v in (params or {}).items() if (p := mdb.check_value(k, v))]
        if bad:
            raise OpError("；".join(bad))
    if virtual is not None and per.virtual != virtual:
        per.set_virtual(virtual[0], virtual[1])
        changes.append(f"{key}.{virtual[0]} = {virtual[1]}")

    for name, value in (params or {}).items():
        if per.get_param(name) == value:
            continue
        per.set_param(name, value)
        changes.append(f"{key}.{name} = {value}")
    return changes


def remove_peripheral(project: IocProject, ip: str, *, clear_pins: bool = True) -> list[str]:
    """移除外设。默认连带清掉引用它的引脚，否则会留下悬空引用。"""
    key = ip.strip()
    if not project.find_peripheral(key):
        raise OpError(f"工程里没有外设 {key}")

    changes: list[str] = []
    for pin in pins_of(project, key):
        if clear_pins:
            project.remove_pin(pin.name)
            changes.append(f"连带移除引脚 {pin.name}")
        else:
            pin.signal = None
            pin.mode = None
            changes.append(f"{pin.name} 的 Signal/Mode 已清空")

    project.remove_peripheral(key)
    changes.append(f"移除外设 {key}")
    return changes


# -------------------------------------------------------------------- GPIO


def add_gpio(
    project: IocProject,
    name: str,
    *,
    mode: str,
    label: str | None = None,
    locked: bool = True,
    props: dict[str, str] | None = None,
) -> list[str]:
    """把引脚配成 GPIO。

    ``mode`` 取 ``Input`` / ``Output`` / ``Analog`` / ``EXTI``。注意 **GPIO 引脚
    不写 ``Mode`` 键**（实测 CubeMX 生成的基准文件只写 ``Signal`` + ``Locked``），
    而且 EXTI 的 signal 是 ``GPXTI<脚号>`` 而不是 ``GPIO_EXTI<n>`` —— 两者都是
    与 CubeMX 输出比对出来的，不是猜的。
    """
    pin_name = resolve_pin(project, name)

    if mode == "EXTI":
        line = sig.pin_number(pin_name)
        signal = f"GPXTI{line}"
    else:
        signal = f"GPIO_{mode}"

    if project.device is not None:
        cap = project.device.mcu.pins.get(pin_name)
        if cap is None:
            raise OpError(f"{pin_name} 不在这颗 MCU 的引脚表里")
        if not cap.can(signal):
            avail = ", ".join(sorted(cap.gpio_modes)) or "无"
            raise OpError(f"{pin_name} 不支持 {mode}（该脚支持：{avail}）")
        _check_gpio_values(project, props)

    pin = project.find_pin(pin_name)
    # Pin 是活视图，不存在时新建一个也只是"指向这些键"的句柄，不产生副作用
    existing_signal = (pin or Pin(project.doc, pin_name)).signal
    # 已经是 GPIO 的引脚属于"改配置"不算"抢"：Output 改 Input、换个标签都是
    # 正经需求。只有真被外设占了才拒绝。
    if existing_signal and not sig.is_gpio_signal(existing_signal):
        owner = _owner_for_signal(project, existing_signal) or existing_signal
        raise OpError(f"{pin_name} 已被 {owner} 占用（{existing_signal}），先 clear-pin 或改别的脚")

    changes: list[str] = []
    if pin is None:
        pin = project.add_pin(pin_name)
        changes.append(f"新增引脚 {pin_name}")
    pin.clear_config()
    pin.signal = signal
    pin.mode = None
    changes.append(f"{pin_name}.Signal = {signal}")
    if label:
        pin.label = label
        changes.append(f"{pin_name}.GPIO_Label = {label}")
    if locked:
        pin.locked = True
        changes.append(f"{pin_name}.Locked = true")
    for key, value in (props or {}).items():
        pin.set_property(key, value)
        changes.append(f"{pin_name}.{key} = {value}")
    return changes


# ---------------------------------------------------------------- 意图级 add


@dataclass
class Allocation:
    """一个信号落到哪个引脚。"""

    signal: str  # 短名，如 TX
    full: str  # 完整名，如 USART2_TX
    pin: str | None  # 虚拟信号为 None
    virtual: bool = False
    source: str = "auto"  # auto / given / reused

    def __str__(self) -> str:
        if self.virtual:
            return f"{self.full}（虚拟，不占引脚）"
        return f"{self.full} -> {self.pin}" + ("" if self.source == "auto" else f"（{self.source}）")


@dataclass
class AddResult:
    ip: str
    mode: str
    virtual: tuple[str, str] | None
    allocations: list[Allocation] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)


def list_modes(project: IocProject, ip: str) -> list[tuple[str, str | None, tuple[str, ...]]]:
    """列出某外设可选的模式：``(名字, 虚拟模式值, 必需信号)``。"""
    device = _required_device(project, "列出模式")
    mdb = device.modes(ip)
    if mdb is None:
        raise OpError(f"{ip} 没有模式库，无法做意图级配置")
    out = []
    for m in mdb.concrete_modes():
        if not m.signals:
            continue
        v = m.virtual
        out.append((m.name, f"{v[0]}={v[1]}" if v else None, m.signals))
    return out


def add_peripheral(
    project: IocProject,
    ip: str,
    *,
    mode: str | None = None,
    pins: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    virtual: tuple[str, str] | None = None,
    force: bool = False,
) -> AddResult:
    """一条命令配好一个外设。

    ``pins`` 是「短信号名 -> 引脚名」，如 ``{"TX": "PA2", "RX": "PA3"}``。没给的
    信号会自动挑一个未被占用、且该脚确实能承载它的引脚；挑不到就整体失败并说明
    是哪些脚本可以、被谁占了。

    模式没给时按 `ModeDb.default_mode` 选（USART -> Asynchronous，
    SPI -> Full_Duplex_Master）—— 依据是模式库里声明的默认虚拟模式值。
    """
    device = _required_device(project, "意图级配置")
    key = ip.strip()

    ref = device.mcu.ip(key)
    if ref is None:
        hint = _near_ip_hint(device, key)
        raise OpError(f"这颗 MCU 上没有外设 {key}{hint}")

    mdb = device.modes(key)
    if mdb is None:
        raise OpError(
            f"{key} 没有模式库（{ref.modes_file_name}），无法推断需要哪些信号。"
            "请改用 set-pin / set-peripheral 逐字段配置。"
        )

    # ---- 模式
    mode_def = None
    if mode is not None:
        mode_def = mdb.get(mode)
        if mode_def is None:
            names = ", ".join(m.name for m in mdb.concrete_modes())
            raise OpError(f"{key} 没有模式 {mode!r}。可选：{names}")
    else:
        mode_def = mdb.default_mode()
        if mode_def is None:
            names = ", ".join(m.name for m in mdb.concrete_modes())
            raise OpError(f"无法自动推断 {key} 的模式，请用 --mode 指定。可选：{names}")

    need = mode_def.signals
    if not need:
        raise OpError(
            f"模式 {mode_def.name} 在模式库里没有声明信号需求，无法自动分配引脚。"
            "请用 --<信号名> 逐个指定。"
        )

    # ---- 虚拟模式键：模式定义了它就必须写，否则工程自相矛盾
    virt = virtual if virtual is not None else mode_def.virtual
    if virt is not None and virt[0] not in ("VirtualMode", "VirtualType"):
        raise OpError(f"外设级模式键只能是 VirtualMode / VirtualType，不是 {virt[0]!r}")

    # ---- 逐个信号定引脚
    given = {k.strip().upper(): v for k, v in (pins or {}).items()}
    # 给了模式不认识的信号名（--rr 少打一个字母）就直接说，别当没看见、悄悄按
    # 默认分配走完 —— 那样用户以为自己的引脚生效了，实际没有。
    unknown_signals = sorted(k for k in given if k not in set(need))
    if unknown_signals:
        raise OpError(
            f"{key} 的模式 {mode_def.name} 只需要 {', '.join(need)} 这些信号，"
            f"不认识 {', '.join(unknown_signals)}"
        )
    busy = _busy(project)
    allocations: list[Allocation] = []
    changes: list[str] = []
    taken: set[str] = set()

    for short in need:
        full = f"{key}_{short}"

        if short.startswith(VIRTUAL_SIGNAL_PREFIX):
            allocations.append(Allocation(short, full, None, virtual=True))
            continue

        existing = next(
            (p for p in pins_of(project, key) if p.signal == full), None
        )
        if short in given:
            pin_name = resolve_pin(project, given.pop(short))
            if not device.mcu.can_pin_do(pin_name, full):
                opts = device.mcu.pins_for(key, short)
                raise OpError(
                    f"{pin_name} 无法承载 {full}。"
                    + (f"可以用的脚：{', '.join(opts)}" if opts else "这颗 MCU 上没有任何脚支持它")
                )
            # 用户显式指定的脚：只拒绝「已经装了别的东西」的，允许一个只在
            # 清单里挂着、还没配的脚 —— 显式指定就是显式授权。
            occupant = (project.find_pin(pin_name) or Pin(project.doc, pin_name)).signal
            if occupant and occupant != full:
                raise OpError(
                    f"{pin_name} 已被 {_owner_for_signal(project, occupant) or occupant} "
                    f"占用（{occupant}）"
                )
            allocations.append(Allocation(short, full, pin_name, source="指定"))
        elif existing is not None:
            allocations.append(Allocation(short, full, existing.name, source="已在工程里"))
        else:
            # pins_for 用「前缀 + 尾段精确相等」匹配，已经排除 USART10 吞掉
            # USART1 的情况；再用 PinCap.can 复核一遍完整 signal 名。
            candidates = [
                p for p in device.mcu.pins_for(key, short)
                if p not in taken
                and device.mcu.can_pin_do(p, full)
                and _is_free(project, p, busy)
            ]
            if not candidates:
                allp = device.mcu.pins_for(key, short)
                used = [p for p in allp if p in busy]
                raise OpError(
                    f"没有空闲引脚可以给 {full}。"
                    + (f"可用脚 {', '.join(allp)} 中，" if allp else "")
                    + (f"{', '.join(used)} 已被占用。" if used else "该信号无可用引脚。")
                    + "用 --%s <引脚> 显式指定。" % short.lower()
                )
            allocations.append(Allocation(short, full, candidates[0], source="auto"))
            taken.add(candidates[0])

    # ---- 参数取值先核对：错了直接拒，报错里带可选值（到这里还没动工程）
    bad = [p for k, v in (params or {}).items() if (p := mdb.check_value(k, v))]
    if bad:
        raise OpError("；".join(bad))

    # ---- 落库（到这里才动工程，前面全是检查）
    for alloc in allocations:
        if alloc.virtual:
            vp = project.add_pin(f"VP_{key}_{alloc.signal}")
            vp.signal = alloc.full
            # 虚拟引脚的 Mode 不是"这个外设的模式"，而是**哪个模式声明了这个
            # VS 信号** —— 两者常常不同名。实测 TIM3 写 Enable_Timer 会被
            # CubeMX 改写成 Internal，而漏写 Mode 会让 TIM3 整个从 Mcu.IP*
            # 里消失（见 `ModeDb.virtual_pin_mode`）。
            vp_mode = mdb.virtual_pin_mode(alloc.signal) or mode_def.name
            vp.mode = vp_mode
            changes.append(f"新增虚拟引脚 {vp.name}（{alloc.full}）")
            changes.append(f"{vp.name}.Mode = {vp_mode}")
            continue
        pin = project.find_pin(alloc.pin or "")
        if pin is None:
            pin = project.add_pin(alloc.pin or "")
        pin.signal = alloc.full
        pin.mode = mode_def.name
        pin.locked = True
        changes.append(f"{pin.name}.Signal = {alloc.full}")
        changes.append(f"{pin.name}.Mode = {mode_def.name}")

    per = project.add_peripheral(key)
    if virt is not None:
        per.set_virtual(virt[0], virt[1])
        changes.append(f"{key}.{virt[0]} = {virt[1]}")

    # ---- 参数
    allowed = mdb.mode_params(mode_def.name)
    # 取值在库里有明确清单/范围的，错了直接拒（报错里带可选值，一次就能改对）；
    # 参数**名**对不上模式只告警照写 —— 模式参数表按条件合并，名字清单不一定全。
    for pname, pvalue in (params or {}).items():
        if not force and allowed and pname not in allowed:
            suggest = ", ".join(sorted(p for p in allowed if p.lower().startswith(pname.lower()[:3])))
            changes.append(
                f"⚠ 模式 {mode_def.name} 的参数表里没有 {pname!r}，仍照写"
                + (f"（相近：{suggest}）" if suggest else "")
            )
        per.set_param(pname, pvalue)
        changes.append(f"{key}.{pname} = {pvalue}")
        if (hint := mdb.check_range(pname, pvalue)):
            changes.append(f"⚠ {hint}")

    return AddResult(ip=key, mode=mode_def.name, virtual=virt,
                     allocations=allocations, changes=changes)


# --------------------------------------------------------- 中断（NVIC）：逐字段

#: ``.ioc`` 里值的冒号是**转义**过的（``true\:0\:0``）。这一节全部按转义态读写，
#: 尾巴就能逐字节原样带走 —— 不解码、不重建，也就没有"转义得对不对"的问题。
_IRQ_SEP = "\\:"


def _irq_db(project: IocProject, what: str) -> IrqDb:
    """中断名的权威来源。拿不到就报错，**不猜**。

    "这颗 MCU 没有中断表"和"这个中断不存在"是两回事，报错必须分开说 ——
    数据库里 2123 颗 MCU 只有 69 颗带 NVIC 表，缺表是**常态**，不是故障。
    """
    device = project.device
    if device is None:
        raise OpError(
            f"{what}需要 CubeMX 数据库来核对中断名和优先级范围。"
            r"设置 ST_CUBEMX_DB 指向 <STM32CubeMX>\db\mcu（或跑 cubemx-cli config --set-db）。"
        )
    db = device.irqs()
    if db is None:
        raise OpError(
            f"这颗 MCU（{project.mcu_name}）的数据库里没有中断表，没法核对 {what}。"
            "这是**缺表**，不是「这颗 MCU 没有这个中断」—— 2123 颗 MCU 里只有 69 颗带表。"
        )
    return db


def _irq_stem(name: str) -> str:
    """``USART1_IRQn`` -> ``USART1``；``DMA2_Stream2_IRQn`` -> ``DMA2_Stream2``。

    **下划线要连着剥掉。** 库和文件里的键名一律是 ``<本体>_IRQn``，只剥 ``IRQn``
    会剩一个尾巴下划线，于是 `_stream_irq_key` 按 stream 找中断永远找不着 ——
    而且找不着是静默的（"这个 stream 没有中断"），正是这份文件最该避免的失败方式。
    """
    for suffix in ("_" + _IRQ_SUFFIX, _IRQ_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _near_irq_hint(irq_db: IrqDb, key: str, limit: int = 8) -> str:
    """中断名打错时的"相近的有：…"，没有候选就返回空串。

    和外设名同款两档，但比的是**去掉 ``_IRQn`` 的干名**：``USART_IRQn``、
    ``USART1_IRQ`` 这两种最常见的错法，拿整名去比编辑距离会全军覆没。

    **大小写分两步走。** 中断名本身大小写敏感（``NVIC.`` 的键名就长那样，查不到
    就该拒绝），所以先按**原样**比 —— 老行为一字不改；只有原样比一无所获时才折成
    小写再比一遍。实测：完全不做这一步的话，``usart1_irqn`` 这种整串小写的写法
    **一个候选都给不出**，而那恰恰是最常见的一种错法（名字容易被小写化）。
    """
    lines = list(irq_db.irqs)
    stems = {n: _irq_stem(n) for n in lines}
    lower = {n: s.lower() for n, s in stems.items()}
    stem = _irq_stem(key)

    def by_prefix(want: str, table: dict[str, str]) -> list[str]:
        # 前缀那一档：只比前 3 个字符，多给几个兄弟总比一个不给强
        head = want[:3]
        return [n for n in lines if table[n].startswith(head)] if len(head) >= 3 else []

    def by_distance(want: str, table: dict[str, str]) -> list[str]:
        hits = set(get_close_matches(want, list(table.values()), n=limit * 2, cutoff=0.6))
        # 干名可能对应多条（``DMA1_Stream0`` 那种不会重名，但共享向量会），
        # 按原表顺序把命中过的整名全部取出，结果稳定。
        return [n for n in lines if table[n] in hits]

    near = (
        by_prefix(stem, stems)
        or by_distance(stem, stems)
        or by_prefix(stem.lower(), lower)
        or by_distance(stem.lower(), lower)
    )
    return f"，相近的有：{', '.join(near[:limit])}" if near else ""


def _int_at(parts: list[str], i: int) -> int | None:
    if len(parts) > i and parts[i].strip().isdigit():
        return int(parts[i])
    return None


def enable_irq(
    project: IocProject,
    name: str,
    *,
    preemption: int | None = None,
    sub: int | None = None,
) -> list[str]:
    """开启一条中断。

    ``.ioc`` 里的真身是 ``NVIC.<IRQn>=true:<抢占>:<子>:<尾巴…>``，而**尾巴是变长的
    （实测 3～10 段）**，它是 CubeMX 的版本印记 —— 同一颗 MCU 的同一个中断名在不同
    版本的文件里尾巴都不一样，甚至同一版本的两份文件也能不同。所以：

    * 键**已存在** → 只改前 3 段，尾巴逐字节原样带回去；
    * 键**不存在** → 写最小的三段式，**绝不自己编尾巴**。实测写 ``true:7:0`` 进去，
      CubeMX 加载后会自己补成 ``true:7:0:false:false:true:true:true:true``。

    优先级的合法区间来自数据库（``NVIC.PriorityGroup`` 决定分组），未给的分量沿用
    现值，没有现值取 0。已经在目标状态的（幂等）返回空清单。
    """
    irq_db = _irq_db(project, f"开启中断 {name}")
    if irq_db.get(name) is None:
        raise OpError(f"这颗 MCU 上没有中断 {name}{_near_irq_hint(irq_db, name)}")

    key = f"NVIC.{name}"
    group = project.doc.get("NVIC.PriorityGroup") or irq_db.priority_group
    pre_lo, pre_hi, sub_lo, sub_hi = irq_db.priority_range(group)
    if preemption is not None and not pre_lo <= preemption <= pre_hi:
        raise OpError(
            f"抢占优先级 {preemption} 超出范围：{group} 下只能是 {pre_lo}..{pre_hi}"
        )
    if sub is not None and not sub_lo <= sub <= sub_hi:
        raise OpError(
            f"子优先级 {sub} 超出范围：{group} 下只能是 {sub_lo}..{sub_hi}"
        )

    raw = project.doc.get_raw(key)
    parts = raw.split(_IRQ_SEP) if raw is not None else []
    cur_pre = _int_at(parts, 1)
    cur_sub = _int_at(parts, 2)
    new_pre = preemption if preemption is not None else (cur_pre or 0)
    new_sub = sub if sub is not None else (cur_sub or 0)
    tail = parts[3:]
    value = _IRQ_SEP.join(["true", str(new_pre), str(new_sub), *tail])

    if value == raw:
        return []
    project.doc.set_raw(key, value)
    shown = f"true:{new_pre}:{new_sub}"
    if tail:
        shown += f":…（后 {len(tail)} 段原样保留）"
    return [f"NVIC.{name} = {shown}"]


def disable_irq(project: IocProject, name: str) -> list[str]:
    """关闭一条中断 —— **删掉那一行**，不是写 ``false``。

    实测：手写 ``NVIC.X_IRQn=false:0:0:…`` 进去，CubeMX 加载后整行都没了。
    "关"在 ``.ioc`` 里的真身就是"没有这一行"，写 ``false`` 只是绕一圈回到同一处。
    """
    irq_db = _irq_db(project, f"关闭中断 {name}")
    if irq_db.get(name) is None:
        raise OpError(f"这颗 MCU 上没有中断 {name}{_near_irq_hint(irq_db, name)}")
    if project.doc.discard(f"NVIC.{name}"):
        return [f"关闭 {name}（删掉 NVIC.{name} 这一行）"]
    return []


def _stream_irq_key(project: IocProject, stream: str) -> str | None:
    """文件里属于这个 stream 的中断**键**（``NVIC.DMA2_Stream2_IRQn``）；没有就 None。

    在**已有的键**里找，不拼名字 —— 拿一个拼出来的名字去删，删不到时看不出是
    "拼错了"还是"本来就没开"。这里只用于"顺手关掉"，所以宁可找不到。
    """
    for key in project.doc.keys_with_prefix("NVIC."):
        name = key[len("NVIC.") :]
        if "." not in name and name.endswith(_IRQ_SUFFIX) and _irq_stem(name) == stream:
            return key
    return None


# ------------------------------------------------------------------ DMA 请求

#: ``Dma.<请求>.<槽位>.<参数>`` —— 槽位是全局索引，和 ``Dma.Request<n>`` 里的 n 同源。
_DMA_SLOT_RE = re.compile(r"Dma\.([A-Za-z0-9_]+)\.(\d+)\.(\w+)")

#: 库里的派生项，**不能写进文件**：``Instance`` 由 `--stream` 表达、``IpInstance`` 是
#: 归属标记、``DMA_Handle`` 是 CubeMX 生成代码时的句柄名（实测库里对它"定死"的取值
#: 就是 ``hdmarx`` —— 那是个 C 变量名，不是配置）。它们会混在 `DmaRequest.fixed` 里，
#: 所以过滤要在这里做一次。
_DMA_DERIVED = frozenset({"Instance", "IpInstance", "DMA_Handle"})


def _dma_db(project: IocProject, what: str) -> DmaDb:
    device = project.device
    if device is None:
        raise OpError(
            f"{what}需要 CubeMX 数据库来确认 DMA 请求名和可用的 stream。"
            r"设置 ST_CUBEMX_DB 指向 <STM32CubeMX>\db\mcu（或跑 cubemx-cli config --set-db）。"
        )
    db = device.dma()
    if db is None:
        raise OpError(
            f"这颗 MCU（{project.mcu_name}）的数据库里没有 DMA 请求表，没法核对 {what}。"
            "这是**缺表**，不是「这颗 MCU 没有这个请求」。"
        )
    return db


def _near_request_hint(dma_db: DmaDb, key: str, owner: str | None = None) -> str:
    """请求名打错时的候选：先列**这个外设**自己的请求，再按编辑距离兜底。"""
    if owner:
        mine = [r.name for r in dma_db.requests_for(owner)]
        if mine:
            return f"，{owner} 可用：{', '.join(mine)}"
    near = get_close_matches(key, list(dma_db.requests), n=6, cutoff=0.6)
    return f"，相近的有：{', '.join(near)}" if near else ""


def _dma_count(project: IocProject) -> int:
    """``Dma.RequestsNb``；读不出或对不上就按 ``Dma.Request<n>`` 的条数算。"""
    n = _int_at([project.doc.get("Dma.RequestsNb") or ""], 0)
    if n is not None:
        return n
    return len(_dma_state(project))


def _existing_param_order(project: IocProject) -> list[str]:
    """工程里已有的 ``RequestParameters=`` 名字表，按首次出现顺序去重合并。

    拿它当模板，而不是拿库里的 `DmaRequest.params`：库里那份是按 ``BaseMode`` 展开
    的，比 CubeMX 实际写进文件的**多**。实测样本工程里 ``USART1_RX`` 的
    ``RequestParameters`` 是 9 个名字，库却给出 14 个 —— 多出来的 ``Channel`` /
    ``DMA_Handle`` / ``FIFOThreshold`` / ``MemBurst`` / ``PeriphBurst`` 这些
    CubeMX 从来不写。照库里写就是往用户的文件里塞一堆多余的键。

    合并多份而不是只取第一份：不同请求的参数集可能确实不同（有的带 ``Channel``），
    取并集才不会漏掉这颗 MCU 真正用到的那些。顺序也不重要 —— 用第一份的顺序即可，
    CubeMX 在不在意这个顺序没实测过，能照抄它就照抄。
    """
    out: list[str] = []
    for key in project.doc.keys_with_prefix("Dma."):
        if not key.endswith(".RequestParameters"):
            continue
        for pname in (project.doc.get(key) or "").split(","):
            if pname and pname not in out:
                out.append(pname)
    return out


def add_dma(
    project: IocProject,
    request: str,
    *,
    stream: str | None = None,
    params: dict[str, str] | None = None,
    irq: bool = True,
    preemption: int | None = None,
) -> list[str]:
    """给一个外设挂上一条 DMA 请求。

    ``--stream`` 没给就自动挑**第一个没被本工程其它请求占用**的，并把挑中的那个打进
    改动清单。固定项（``Direction`` 这类硬件定死的）取库里的值，可调项取 ``--param``，
    没给就用库默认值。
    """
    dma_db = _dma_db(project, f"配置 DMA 请求 {request}")
    req = dma_db.request(request)
    if req is None:
        owner = _owner_hint_from_request(request)
        raise OpError(
            f"这颗 MCU 上没有 DMA 请求 {request}{_near_request_hint(dma_db, request, owner)}"
        )

    state = _dma_state(project)
    if request in state:
        idx, current, _ = state[request]
        # 这是个**拒绝**，不是"没有改动"：用户可能正想换 stream 或改参数，静默返回
        # 空清单会让他以为改好了。
        raise OpError(
            f"DMA 请求 {request} 已经挂在 {current or '（没指定 stream）'} 上（槽位 {idx}）。"
            "要改先 remove-dma 再 add-dma。"
        )

    taken = {s for _, s, _ in state.values() if s}
    if stream is None:
        if not req.streams:
            # **不能报"都被占用了"** —— 那是在说一件没核对过的事。实测带 DMAMUX 的
            # 型号（STM32WB5MMGHx 等）在库里**一个候选通道都没有**：这类 MCU 的
            # 请求→通道映射是 DMAMUX 随时指定的，ST 的数据库里压根没有那棵树
            # （XML 里连 `DMA*_Stream*` / `DMA*_Channel*` 形态的 Mode 都不存在）。
            # 报成"占用冲突"会让用户去查一个根本不存在的冲突。
            raise OpError(
                f"{request} 没有可自动挑选的通道：库里对它没给任何候选"
                f"（可调参数有 {len(req.settable)} 个，通道名一个都没有）。"
                "带 DMAMUX 的型号属于这一类 —— 请用 --stream 指名一个通道"
                "（同型号已有工程里 `Dma.<请求>.<n>.Instance` 的取值可作参照）。"
            )
        free = [s for s in req.streams if s not in taken]
        if not free:
            raise OpError(
                f"{request} 没有空闲的 stream：可选 {', '.join(req.streams)}，"
                f"但都被占用了（{', '.join(sorted(taken))}）"
            )
        stream = free[0]
    elif stream not in req.streams:
        raise OpError(
            f"{request} 不能挂在 {stream} 上。可选：{', '.join(req.streams) or '（库里的可选清单是空的）'}"
        )

    supplied = dict(params or {})
    warnings: list[str] = []
    for pname, pvalue in supplied.items():
        if pname not in req.settable:
            warnings.append(
                f"⚠ {request} 的可设参数里没有 {pname!r}，仍照写"
                f"（可设：{', '.join(req.settable) or '无'}）"
            )
            continue
        d = dma_db.param(pname)
        if d is not None and d.values and pvalue not in d.values:
            raise OpError(f"{pname}={pvalue} 不在取值范围内。可选：{', '.join(d.values)}")

    # 值：Instance + 硬件定死的 + 有默认值的可调项 + 用户点名要的。
    # 派生项（`IpInstance` / `DMA_Handle`）不写 —— 它们是 CubeMX 生成物，不是配置。
    values: dict[str, str] = {
        "Instance": stream,
        **{k: v for k, v in req.fixed_map.items() if k not in _DMA_DERIVED},
    }
    for pname in req.params:
        if pname not in req.settable or pname in values or pname in supplied:
            continue
        default = dma_db.default_value(pname)
        # 库里有些参数的默认值字面就写着 `null`（XML 表达"没有默认"的方式）。
        # 那不是能写进 .ioc 的取值。
        if default is not None and default != "null":
            values[pname] = default
    values.update(supplied)

    # 参数清单：**照工程里已有的抄**（那是 CubeMX 自己写的），没有才退回库里的清单。
    order = _existing_param_order(project)
    if order:
        names = [p for p in order if p in values]
        # 用户显式点的参数必须留 —— 哪怕模板里没有它（他比模板更清楚自己要什么）。
        names += [p for p in supplied if p not in names and p in values]
    else:
        # 工程里一个 DMA 请求都没有，没得抄 —— 只能照库里的清单写。这是**唯一没实测过**
        # 的一支：库里比 CubeMX 实际写的多几个（`MemBurst`/`PeriphBurst` 这些，
        # 实测样本里 CubeMX 就不写）。多半无害（CubeMX 会按自己的规矩规范化掉），
        # 但没验过就不当它一定对 —— 拿 `--verify` 跑一次就有答案。
        names = list(values)

    idx = _dma_count(project)
    changes = [f"{request} → {stream}（槽位 {idx}）", *warnings]
    project.doc.set(f"Dma.Request{idx}", request)
    project.doc.set("Dma.RequestsNb", str(idx + 1))
    for pname in names:
        project.doc.set(f"Dma.{request}.{idx}.{pname}", values[pname])
    project.doc.set(f"Dma.{request}.{idx}.RequestParameters", ",".join(names))

    # DMA 控制器本身要登记进 Mcu.IP*，否则 CubeMX 不会初始化它。实测：17/17 份
    # 带 Dma.Request<n> 的官方板级文件里 DMA 都在 Mcu.IP* 里。
    if project.find_peripheral("DMA") is None:
        project.add_peripheral("DMA")
        changes.append("登记外设 DMA")

    # 反向的 functionlistsort **不写**：那 17 份文件里没有一份含 MX_DMA_Init，
    # 而往返实测里 CubeMX 自己会按配置把它算出来 —— 手写只会制造冲突。

    if irq:
        changes += _attach_stream_irq(project, stream, preemption)
    return changes


def _attach_stream_irq(
    project: IocProject, stream: str, preemption: int | None
) -> list[str]:
    """顺带开这个 stream 的中断。名字走**查表**，查不到就不开（并说明）。"""
    device = project.device
    irq_db = device.irqs() if device is not None else None
    if irq_db is None:
        return [f"（没开 {stream} 的中断：数据库里没有中断表）"]
    found = irq_db.for_stream(stream)
    if found is None:
        return [f"（没开 {stream} 的中断：中断表里没有它）"]
    return enable_irq(project, found.name, preemption=preemption)


def _owner_hint_from_request(request: str) -> str | None:
    """``USART1_RX`` -> ``USART1`` —— 只用来让报错多列几行候选。"""
    head = request.split("_", 1)[0]
    return head or None


def remove_dma(project: IocProject, request: str, *, irq: bool = True) -> list[str]:
    """删掉一条 DMA 请求。

    这是唯一会**毁数据**的方向，麻烦在索引是耦合的：``Dma.Request<n>`` 和
    ``Dma.<请求>.<n>.*`` 里的 ``n`` 是**同一个数**，删掉中间一条，后面所有请求的
    名字键和槽位键都得整体前移一位。
    """
    state = _dma_state(project)
    if request not in state:
        dma_db = project.device.dma() if project.device is not None else None
        hint = (
            _near_request_hint(dma_db, request, _owner_hint_from_request(request))
            if dma_db is not None
            else ""
        )
        raise OpError(f"工程里没有 DMA 请求 {request}{hint}")

    idx, stream, _ = state[request]
    doc = project.doc
    changes = [f"删除 DMA 请求 {request}（原来在槽位 {idx}，stream {stream or '未指定'}）"]

    # 「先全删、再全写」两个阶段，而不是"按序改名"。
    #
    # 改名要论证顺序（升序碰巧对、降序互相覆盖），而论证依赖"每个请求恰好一个槽位"
    # 这条实测结论；两个阶段的做法**不依赖任何前提**：中间过程里没有任何一个键被
    # 两个来源争抢，也不可能留下"改了一半"的半截映射。
    #
    # 取值必须走 get_raw / set_raw：`entries` 存的是**转义态**，用 get/set 兜一圈会把
    # `\:` 二次转义成 `\\:` —— 一条真正的静默损坏。
    doomed: list[str] = [f"Dma.Request{idx}", *doc.keys_with_prefix(f"Dma.{request}.{idx}.")]
    moves: list[tuple[str, str, str]] = []

    for key in doc.keys_with_prefix("Dma.Request"):
        raw_n = key[len("Dma.Request") :]
        if raw_n.isdigit() and int(raw_n) > idx:
            moves.append((key, f"Dma.Request{int(raw_n) - 1}", doc.get_raw(key) or ""))

    for key in doc.keys_with_prefix("Dma."):
        m = _DMA_SLOT_RE.fullmatch(key)
        if m is None:
            continue
        name, slot = m.group(1), int(m.group(2))
        if name == request or slot <= idx:
            continue
        moves.append((key, f"Dma.{name}.{slot - 1}.{m.group(3)}", doc.get_raw(key) or ""))

    for key in doomed:
        doc.discard(key)
    for old, _, _ in moves:
        doc.discard(old)
    for _, new, value in moves:
        doc.set_raw(new, value)

    # 条数按**结果**重算，不是 N-1 —— 文件里本来有洞时 N-1 会写错。
    remaining = len(_dma_state(project))
    if remaining:
        doc.set("Dma.RequestsNb", str(remaining))
    else:
        # 一个请求都不剩了：**删掉 RequestsNb，而不是写 0**。实测 524 份 ST 官方板级
        # 文件里 RequestsNb 出现过 17 次（值 1/2/3/6/7/9），**0 次都没出现过** ——
        # "没有 DMA"在这份文件里的真身是"整块 Dma.* 都不存在"。这和"关中断 = 删行"
        # 是同一条规矩：缺省态永远表达成"键不在"。
        doc.discard("Dma.RequestsNb")
        changes.append("Dma.RequestsNb 已删除（没有 DMA 请求了）")
        # DMA 从外设清单里一起退场。实测：17/17 份有 Dma.* 的文件都登记了 Mcu.IP*=DMA，
        # 507/507 份没有 Dma.* 的都没登记 —— 这条相关性是完全的。
        if project.find_peripheral("DMA") is not None:
            project.remove_peripheral("DMA")
            changes.append("移除外设 DMA")

    # 释放出来的那个 stream 的中断顺手关掉，和 add_dma 的默认开对称。同一个 stream
    # 还被别的请求用着就留着 —— 删掉它等于删掉那个请求的中断。
    # **绝不写 =false**：那会是个 CubeMX 自己都不认的中间态（"关"就是删行）。
    if irq and stream and not any(s == stream for _, s, _ in _dma_state(project).values()):
        key = _stream_irq_key(project, stream)
        if key is not None:
            changes.append(f"关闭 {key[len('NVIC.') :]}（释放了 {stream}）")
            project.doc.discard(key)
    return changes


# ------------------------------------------------------------------ 浏览（只读）


@dataclass(frozen=True)
class InstanceInfo:
    """顶层清单里的一行。"""

    name: str
    configured: bool = False
    #: 已配置时才有意义
    virtual: tuple[str, str] | None = None
    pins: tuple[str, ...] = ()
    params: int = 0
    irqs: int = 0
    dma: int = 0


@dataclass(frozen=True)
class InstanceList:
    """`list <工程>` 的结果。"""

    mcu: str
    instances: tuple[InstanceInfo, ...]
    #: False = 没数据库，清单里**只有工程自己声明过的外设**，不是这颗 MCU 的全部
    complete: bool = True

    @property
    def configured(self) -> tuple[InstanceInfo, ...]:
        return tuple(i for i in self.instances if i.configured)

    @property
    def others(self) -> tuple[InstanceInfo, ...]:
        return tuple(i for i in self.instances if not i.configured)


@dataclass(frozen=True)
class ModeRow:
    name: str
    virtual: str | None      # 能直接喂回去的值，如 VM_ASYNC
    virtual_key: str | None  # VirtualMode / VirtualType
    signals: tuple[str, ...]
    current: bool = False


@dataclass(frozen=True)
class ParamRow:
    name: str
    value: str | None        # 工程里的值；没设过就是库里的默认值
    set: bool                # 工程里显式设过吗
    default: str | None = None
    values: tuple[str, ...] = ()
    comment: str | None = None


@dataclass(frozen=True)
class IrqRow:
    name: str
    comment: str | None = None
    enabled: bool = False
    preemption: int | None = None
    sub: int | None = None
    #: True = 这个中断本来就属于本外设；False = 随某个 DMA 请求一起出现的 stream 中断
    owned: bool = True
    from_dma: str | None = None
    condition: str | None = None


@dataclass(frozen=True)
class NvicPage:
    priority_group: str | None
    preemption: tuple[int, int]
    sub: tuple[int, int]
    irqs: tuple[IrqRow, ...]

    @property
    def enabled(self) -> tuple[IrqRow, ...]:
        return tuple(i for i in self.irqs if i.enabled)

    @property
    def settable(self) -> tuple[IrqRow, ...]:
        """本外设自己的、目前还没开的中断。"""
        return tuple(i for i in self.irqs if i.owned and not i.enabled)


@dataclass(frozen=True)
class DmaRow:
    request: str
    streams: tuple[str, ...]
    #: 工程里现在挂的 stream；没配就是 None
    current: str | None = None
    #: 库里写死的取值（Channel/Direction/…）
    fixed: tuple[tuple[str, str], ...] = ()
    #: 工程里现在的参数值
    params: tuple[tuple[str, str], ...] = ()
    #: 还能改的参数名
    settable: tuple[str, ...] = ()


@dataclass(frozen=True)
class PinRow:
    pin: str
    signal: str | None = None
    mode: str | None = None
    label: str | None = None
    #: 工程里现在的 GPIO 参数
    params: tuple[tuple[str, str], ...] = ()
    #: 这颗脚现在这个模式下能改的 GPIO 参数名
    settable: tuple[str, ...] = ()
    #: 可设参数 -> (可选值, 默认值)；库里没有枚举的参数不在这里
    options: tuple[tuple[str, tuple[str, ...], str | None], ...] = ()


@dataclass(frozen=True)
class InspectResult:
    """`list <工程> <外设>` 的结果。没数据库的页在 `missing` 里点名。"""

    ip: str
    kind: str | None = None
    version: str | None = None
    mode: str | None = None
    virtual: tuple[str, str] | None = None
    configured: bool = False
    pins: tuple[PinRow, ...] = ()
    modes: tuple[ModeRow, ...] = ()
    params: tuple[ParamRow, ...] = ()
    nvic: NvicPage | None = None
    dma: tuple[DmaRow, ...] = ()
    #: 因为缺数据而**看不了**的页（不是"空"）。存的是**页名**（``mode`` /
    #: ``param`` / ``nvic`` / ``dma``），不是给人看的标签 —— 这样 `--json` 的调用方
    #: 能直接 ``"nvic" in missing``，`has()` 也才有意义。
    missing: tuple[str, ...] = ()

    def has(self, page: str) -> bool:
        """这一页有结论吗。`False` = 读不出来，**不是**"空"。"""
        return page not in self.missing


#: ``NVIC.<名>`` 里，只有名字带这个后缀的才是中断开关。
#:
#: 其余全是普通标量。实测 524 份 ST 官方板级 ``.ioc`` 里出现过 8 种：
#: ``PriorityGroup`` / ``ForceEnableDMAVector`` / ``TimeBase`` / ``TimeBaseIP`` /
#: ``BFHFNMINS`` / ``PRIS`` / ``Saved{Pendsv,Svcall,Systick}IrqHandlerGenerated``。
#:
#: 用**后缀**而不是``{这几个名字}``的白名单：白名单会漂移，而数据库里 69 份
#: NVIC 文件的 5141 条中断**全部**以 ``IRQn`` 结尾 —— 后缀这条规则是整个库验证
#: 过的，白名单只是碰巧覆盖了这批样本。多一个标量键不该被误当成中断，
#: 少一个也不该让真中断被漏掉。
_IRQ_SUFFIX = "IRQn"

#: GPIO 页里永远不该露给用户的参数名。
#:
#: 这个集合是**推演出来的**：GPIO 模式库里 ``<RefMode Name="GPIO">`` 声明了
#: ``GPIOx``/``GPIO_Pin``/``GPIO_AF``（端口名、位掩码、复用功能编号 —— 全是
#: 派生值，不是用户选项），而 ``*Default*`` 那一堆（``GPIO_ModeDefaultOutputPP``
#: 之类）是"选了这个模式就用这个取值"的实现细节，GUI 里也不单独给。
_GPIO_INTERNAL = frozenset({"GPIOx", "GPIO_Pin", "GPIO_AF"})

#: CubeMX 自己合成的键，**任何数据库文件里都查不到**（跟 `GPXTI<脚号>` 同类）。
#: 引脚标签是用户最常设的东西，所以显式补进可设清单并注明来源。
_GPIO_SYNTHETIC = ("GPIO_Label",)


def _gpio_db_mode(pin) -> str | None:
    """引脚在 GPIO 模式库里对应哪个模式：GPIO 脚按 signal（``GPIO_Output`` ->
    ``Output``，``GPXTI3`` -> ``EXTI``），外设脚按复用功能（``GPIO``）。"""
    return sig.gpio_mode_of(pin.signal or "") or pin.mode


def _gpio_settable(gpio_db, mode: str | None) -> tuple[str, ...]:
    """这颗脚在 ``mode`` 下能改哪些 GPIO 参数。

    数据库里 GPIO 的"模式"（``Input``/``Output``/``Analog``/``EXTI``/``GPIO``）
    和引脚上的 ``Mode`` 只在 GPIO 脚上同名 —— 外设脚写的是 ``Asynchronous``
    这类，那时按 ``GPIO`` 那个模式（复用功能）算。

    ``GPIO_ModeDefault*`` **要列出来**：Output 的推挽/开漏、EXTI 的触发沿就是靠
    它们设的（``.ioc`` 里写的正是 ``GPIO_ModeDefaultEXTI``），藏掉它们等于让人
    猜键名。只藏真正的派生值和 LL 专用项。
    """
    if gpio_db is None:
        return _GPIO_SYNTHETIC
    key = mode if mode and gpio_db.get(mode) else "GPIO"
    names = gpio_db.mode_params(key)
    if not names:
        names = gpio_db.mode_params("GPIO")
    keep = sorted(
        n for n in names
        if n not in _GPIO_INTERNAL and not n.endswith("_LL")
    )
    return tuple(keep) + _GPIO_SYNTHETIC


def _gpio_options(gpio_db, names) -> tuple[tuple[str, tuple[str, ...], str | None], ...]:
    if gpio_db is None:
        return ()
    out = []
    for n in names:
        d = gpio_db.param(n)
        if d is not None and d.values:
            out.append((n, d.values, d.default))
    return tuple(out)


def _nvic_state(project: IocProject) -> dict[str, tuple[bool, int | None, int | None]]:
    """``NVIC.<IRQn>`` 键 -> ``(启用, 抢占, 子)``。

    元组是**变长**的（实测 3～10 段），手写的残缺行也认 —— 缺段按"没设"处理，
    不抛异常。浏览命令不该因为一行畸形数据整条挂掉。
    """
    out: dict[str, tuple[bool, int | None, int | None]] = {}
    for key in project.doc.keys_with_prefix("NVIC."):
        name = key[len("NVIC."):]
        if not name.endswith(_IRQ_SUFFIX) or "." in name:
            continue
        raw = project.doc.get(key)
        if raw is None:
            continue
        parts = raw.split(":")
        enabled = bool(parts) and parts[0].strip().lower() == "true"

        def _num(i: int) -> int | None:
            if len(parts) > i and parts[i].strip().isdigit():
                return int(parts[i])
            return None

        out[name] = (enabled, _num(1), _num(2))
    return out


def _dma_state(project: IocProject) -> dict[str, tuple[int, str | None, dict[str, str]]]:
    """``Dma.Request<n>`` -> ``(n, 挂的 stream, 各参数当前值)``。

    ``n`` 是全局索引，``.ioc`` 里每个请求自己的键都带它
    （``Dma.USART1_RX.0.Instance``）—— 索引和请求名对不上就会写错行。
    """
    out: dict[str, tuple[int, str | None, dict[str, str]]] = {}
    for key in project.doc.keys_with_prefix("Dma.Request"):
        raw_idx = key[len("Dma.Request"):]
        if not raw_idx.isdigit():
            continue
        request = project.doc.get(key)
        if not request:
            continue
        idx = int(raw_idx)
        prefix = f"Dma.{request}.{idx}."
        params = {
            k[len(prefix):]: (project.doc.get(k) or "")
            for k in project.doc.keys_with_prefix(prefix)
            if "." not in k[len(prefix):]
        }
        params.pop("RequestParameters", None)
        out[request] = (idx, params.pop("Instance", None), params)
    return out


def list_instances(project: IocProject) -> InstanceList:
    """这颗 MCU 上有哪些外设、各自配到哪一步了（只读）。

    **不需要数据库也能跑**，但那时只能列出工程自己声明过的外设 —— 结果里
    ``complete=False``，调用方必须把这点说出来，不能当成"这颗 MCU 就这么多"。
    """
    device = project.device
    per_map = {p.name: p for p in project.peripherals()}
    pins_by_ip: dict[str, list[str]] = {}
    for pin in project.pins():
        owner = owner_of(project, pin)
        if owner and pin.name:
            pins_by_ip.setdefault(owner, []).append(pin.name)

    if device is None:
        names = sorted(per_map)
        complete = False
    else:
        names = sorted(set(device.mcu.ips) | set(per_map))
        complete = True

    # 顶层每行都要报"开了几个中断、挂了几个 DMA"，这两张表只载一次
    irq_db = device.irqs() if device is not None else None
    dma_db = device.dma() if device is not None else None
    nvic = _nvic_state(project)
    dma_state = _dma_state(project)

    out: list[InstanceInfo] = []
    for name in names:
        per = per_map.get(name)
        if per is None:
            out.append(InstanceInfo(name=name))
            continue
        n_irq = 0
        if irq_db is not None:
            for irq_name, (enabled, _, _) in nvic.items():
                if not enabled:
                    continue
                irq = irq_db.get(irq_name)
                if irq is not None and irq.belongs_to(name):
                    n_irq += 1
        n_dma = 0
        if dma_db is not None:
            for request in dma_state:
                req = dma_db.request(request)
                if req is not None and req.belongs_to(name):
                    n_dma += 1
        out.append(InstanceInfo(
            name=name,
            configured=True,
            virtual=per.virtual,
            pins=tuple(pins_by_ip.get(name, ())),
            params=len(per.parameters),
            irqs=n_irq,
            dma=n_dma,
        ))
    return InstanceList(mcu=project.mcu_name, instances=tuple(out), complete=complete)


def inspect(project: IocProject, ip: str, page: str | None = None) -> InspectResult:
    """一个外设的配置页（只读）：模式 / 参数 / NVIC 中断 / DMA 请求 / 引脚。

    ``page`` 为 None 时全给；给了就只算那一页（省掉无谓的数据库加载）。
    """
    device = _required_device(project, "浏览外设配置")
    key = ip.strip()
    ref = device.mcu.ip(key)
    per = project.find_peripheral(key)
    if ref is None and per is None:
        raise OpError(f"这颗 MCU 上没有外设 {key}{_near_ip_hint(device, key)}")

    missing: list[str] = []
    mdb = device.modes(key)

    #: 外设当前的"模式"以**引脚上的 `Mode`** 为准 —— 外设级的 `VirtualMode`
    #: 是多对一的（`Asynchronous`/`Half_duplex`/`LIN` 全都映射到 `VM_ASYNC`），
    #: 光看它反推不出用户选的是哪一个。没有引脚时退回 `VirtualMode`。
    pin_mode = next((p.mode for p in pins_of(project, key) if p.mode), None)
    virtual = per.virtual if per is not None else None

    # ---- 模式 + 参数
    modes: tuple[ModeRow, ...] = ()
    params: tuple[ParamRow, ...] = ()
    if page in (None, "mode", "param"):
        if mdb is None:
            missing.append("mode")
        else:
            modes = tuple(
                ModeRow(
                    name=m.name,
                    virtual=(v[1] if (v := m.virtual) else None),
                    virtual_key=(v[0] if v else None),
                    signals=m.signals or (),
                    current=(m.name == pin_mode),
                )
                for m in mdb.concrete_modes()
                if m.signals
            )
            if page in (None, "param"):
                # "这个模式允许哪些参数"要按当前模式算；工程没设模式时用库里的默认
                active = mdb.get(pin_mode) if pin_mode else None
                if active is None and virtual is not None:
                    active = next(
                        (m for m in mdb.concrete_modes() if m.virtual == virtual), None
                    )
                if active is None:
                    active = mdb.default_mode()
                if active is None:
                    missing.append("param")
                else:
                    allowed = mdb.mode_params(active.name)
                    set_names = set(per.parameters) if per is not None else set()
                    params = tuple(
                        ParamRow(
                            name=n,
                            value=(per.get_param(n) if per is not None else None)
                            or mdb.default_value(n),
                            set=n in set_names,
                            default=mdb.default_value(n),
                            values=(d.values if (d := mdb.param(n)) else ()),
                            comment=(d.comment if d else None),
                        )
                        for n in sorted(allowed)
                    )

    # ---- 工程里的现状，两个页都要用，只读一次
    dma_now = _dma_state(project) if page in (None, "nvic", "dma") else {}

    # ---- NVIC：本外设自己的中断 + 随 DMA 请求来的 stream 中断
    nvic: NvicPage | None = None
    if page in (None, "nvic"):
        irq_db = device.irqs()
        if irq_db is None:
            missing.append("nvic")
        else:
            state = _nvic_state(project)
            rows: list[IrqRow] = []
            seen: set[str] = set()
            for irq in irq_db.for_instance(key):
                seen.add(irq.name)
                rows.append(_irq_row(irq, state, owned=True))
            # 挂了 DMA 的外设，stream 的中断也在同一页里 —— 用户配 UART 的 DMA
            # 时真正要开的正是这些，漏掉的话这页看着像"没中断可开"。
            dma_db = device.dma()
            if dma_db is not None:
                for request, (_, stream, _) in sorted(dma_now.items()):
                    req = dma_db.request(request)
                    if req is None or not req.belongs_to(key) or not stream:
                        continue
                    irq = irq_db.for_stream(stream)
                    if irq is None or irq.name in seen:
                        continue
                    seen.add(irq.name)
                    rows.append(_irq_row(irq, state, owned=False, from_dma=request))
            group = project.doc.get("NVIC.PriorityGroup") or irq_db.priority_group
            lo, hi, slo, shi = irq_db.priority_range(group)
            nvic = NvicPage(
                priority_group=group,
                preemption=(lo, hi),
                sub=(slo, shi),
                irqs=tuple(rows),
            )

    # ---- DMA 请求
    dma: tuple[DmaRow, ...] = ()
    if page in (None, "dma"):
        dma_db = device.dma()
        if dma_db is None:
            missing.append("dma")
        else:
            rows = []
            for req in dma_db.requests_for(key):
                _, stream, cur_params = dma_now.get(req.name, (None, None, {}))
                rows.append(DmaRow(
                    request=req.name,
                    streams=req.streams,
                    current=stream,
                    fixed=req.fixed,
                    params=tuple(sorted(cur_params.items())),
                    settable=req.settable,
                ))
            dma = tuple(rows)

    # ---- 引脚 + GPIO 参数
    pins: tuple[PinRow, ...] = ()
    if page in (None, "gpio"):
        gpio_db = device.modes("GPIO")
        rows = []
        for pin in project.pins():
            if is_virtual_pin(pin.name):
                continue
            # `list <工程> GPIO`：GPIO 脚（GPIO_Output / GPXTI3 …）没有"归属外设"，
            # 按 owner 过滤会一个都列不出来 —— 它们就是 GPIO 这一页的全部内容。
            if key == "GPIO":
                if not sig.is_gpio_signal(pin.signal or ""):
                    continue
            elif owner_of(project, pin) != key:
                continue
            settable = _gpio_settable(gpio_db, _gpio_db_mode(pin))
            # 只报登记进 `GPIOParameters` 的那些 —— `pin.values` 连 Signal/Mode
            # 这些顶层字段一起给，那不是"GPIO 参数"。
            rows.append(PinRow(
                pin=pin.name,
                signal=pin.signal,
                mode=pin.mode,
                label=pin.label,
                params=tuple(
                    (n, pin.get_property(n) or "") for n in sorted(pin.parameters)
                ),
                settable=settable,
                options=_gpio_options(gpio_db, settable),
            ))
        pins = tuple(rows)

    return InspectResult(
        ip=key,
        kind=ref.name if ref is not None else None,
        version=ref.version if ref is not None else None,
        mode=pin_mode,
        virtual=virtual,
        configured=per is not None,
        pins=pins,
        modes=modes,
        params=params,
        nvic=nvic,
        dma=dma,
        missing=tuple(missing),
    )


def _irq_row(
    irq,
    state: dict[str, tuple[bool, int | None, int | None]],
    *,
    owned: bool,
    from_dma: str | None = None,
) -> IrqRow:
    enabled, pre, sub = state.get(irq.name, (False, None, None))
    return IrqRow(
        name=irq.name,
        comment=irq.comment,
        enabled=enabled,
        preemption=pre,
        sub=sub,
        owned=owned,
        from_dma=from_dma,
        condition=irq.condition,
    )


def can_pin_do(project: IocProject, pin: str, signal: str) -> tuple[bool, str]:
    """``(能不能, 人话的说明)``。没有数据库时无法判断，返回 ``(True, 说明)``。"""
    if project.device is None:
        return True, "（无数据库，未校验）"
    p = resolve_pin(project, pin)
    cap = project.device.mcu.pins.get(p)
    if cap is None:
        return False, f"{p} 不在这颗 MCU 的引脚表里"
    if cap.can(signal):
        return True, ""
    if sig.is_gpio_signal(signal):
        return False, f"{p} 不支持 {signal}（可选：{', '.join(sorted(cap.gpio_modes)) or '无'}）"
    owner = project.device.mcu.signal_owner(signal)
    if owner:
        opts = [q for q in project.device.mcu.pins_for(owner, signal[len(owner) + 1:])]
        return False, f"{p} 不支持 {signal}，该信号可用的脚：{', '.join(opts) or '无'}"
    return False, f"{p} 不支持 {signal}"


__all__ = [
    "AddResult",
    "Allocation",
    "DmaRow",
    "InspectResult",
    "InstanceInfo",
    "InstanceList",
    "IrqRow",
    "ModeRow",
    "NvicPage",
    "OpError",
    "ParamRow",
    "PinRow",
    "ProjectError",
    "add_dma",
    "add_gpio",
    "add_peripheral",
    "can_pin_do",
    "clear_pin",
    "disable_irq",
    "enable_irq",
    "inspect",
    "list_instances",
    "list_modes",
    "owner_of",
    "pins_of",
    "remove_dma",
    "remove_peripheral",
    "resolve_pin",
    "set_peripheral",
    "set_pin",
]
