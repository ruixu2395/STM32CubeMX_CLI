"""命令行入口。

只用一条规则：**改内存 → 校验 → 通过了才落盘**。校验不过就原样退出，磁盘上的
``.ioc`` 一个字节都不变（`IocProject.save` 走的是临时文件 + ``os.replace``，
而且只在全部检查通过后才被调用）。

命令分两档：

* **意图级** —— ``add-peripheral`` / ``add-gpio``。说"我要 USART2 跑
  Asynchronous"，工具查数据库挑引脚、定模式、写全派生键。
* **逐字段** —— ``set-pin`` / ``set-peripheral`` / ``clear-pin`` …
  改单个字段，用来做微调或数据库覆盖不到的冷门外设。

两次都支持 ``--dry-run``：只打印改动和 diff，不碰文件。

**CubeMX 装在哪**存在用户目录里（`settings.py`），第一次运行问一次，`config` 子命令
负责看和改。提示语一律写 stderr —— stdout 是 ``--json`` 的载荷通道。
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Callable

from . import config, cubemx, ops, validate
from . import settings as settings_mod
from .db import DbError, Device
from .ioc.project import IocProject, ProjectError
from .ioc.raw import IocDocument

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INVALID = 2


class CliError(Exception):
    """命令行层面的用法错误（区别于工程本身的问题）。"""


# ------------------------------------------------------------------ 载入/落盘


def _check_root(path: Path) -> None:
    """``ST_CUBEMX_ALLOWED_ROOTS`` 白名单。未设置时不限制。"""
    roots = config.allowed_roots()
    if not roots:
        return
    resolved = path.resolve()
    if not any(resolved == r or r in resolved.parents for r in roots):
        raise CliError(
            f"{path} 不在允许的目录里。当前白名单："
            + "、".join(str(r) for r in roots)
            + "（改 ST_CUBEMX_ALLOWED_ROOTS）"
        )


def _open(path: Path) -> IocProject:
    if not path.exists():
        raise CliError(f"工程不存在：{path}")
    _check_root(path)
    project = IocProject.load(path)
    if not project.mcu_name:
        raise CliError(f"{path} 里没有 Mcu.Name，不像是一份 .ioc 工程")
    return project


def _device(project: IocProject) -> None:
    """给工程挂上权威数据库；挂不上就给个明确的警告，不静默降级。"""
    try:
        project.device = Device.open(project.mcu_name)
    except DbError as exc:
        print(f"警告：{exc}", file=sys.stderr)
        print(
            "      引脚能力、模式、参数这三层校验将跳过；意图级命令不可用。",
            file=sys.stderr,
        )


def _verify_before_write(
    project: IocProject, np: argparse.Namespace, baseline: IocDocument
) -> int | None:
    """``--verify``：落盘前让 CubeMX 亲自把候选工程读一遍再存一遍。

    **排在 `save` 之前**是刻意的，也是本项目最初那条约束的原话（"确认 CubeMX 接受
    这个配置后，再覆盖原文件"）。候选内容渲染到临时目录里的一份副本上，往返在副本上做
    —— 所以"验证不过就不落盘"和既有的"校验不过就不落盘"是同一条保证。

    返回 ``None`` 表示通过（可以继续写盘），否则返回该用的退出码：

    * **没跑成**（没装 CubeMX、超时、没产出）→ ``EXIT_USAGE``。这不是"工程有问题"，
      而是"没验成" —— 归到 2 会让 Agent 去修一个根本没坏的东西。但**也绝不写盘**：
      没验就是不验，和 `validate` 没有数据库时标 `skipped` 是同一条纪律。
    * **跑了、但我们写的键被丢了** → ``EXIT_INVALID``。这才是"校验没过"。
    """
    result = cubemx.verify(
        project,
        exe=config.cube_mx_exe(),
        timeout=np.verify_timeout,
        baseline=baseline,
        keep=np.keep_temp,
    )
    if not result.ran:
        print(f"\n往返验证没跑成，没有写入文件。{result.failure}", file=sys.stderr)
        if result.detail:
            print(result.detail, file=sys.stderr)
        return EXIT_USAGE

    print("\n" + result.round_trip.render(), file=sys.stderr)
    if not result.round_trip.ok:
        print(
            "\nCubeMX 丢掉了上面这些本次写入的键，**没有写入文件**。"
            "常见原因见 references/troubleshooting.md。",
            file=sys.stderr,
        )
        return EXIT_INVALID
    return None


def _apply(
    project: IocProject,
    path: Path,
    mutate: Callable[[], list[str]],
    np: argparse.Namespace,
) -> int:
    """跑一遍改动 → 校验 → 落盘。未通过校验则原文件不动。"""
    before = project.render()
    # 改动前的快照。往返验证要靠它区分"我们写的键"和"本来就有的键" ——
    # **必须在 mutate() 之前取**，之后取到的就是候选本身了。
    baseline = IocDocument(dict(project.doc.entries))

    try:
        changes = mutate()
    except (ops.OpError, ProjectError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    report = validate.validate(project)
    blocking = validate.blocking_errors(report)

    if changes:
        for c in changes:
            print(f"  {c}")
        # 后面报告走 stderr（无缓冲），不 flush 的话管道里会跑到改动清单前面
        sys.stdout.flush()

    if np.dry_run:
        _print_diff(before, project.render())
        print("（--dry-run：没有写入文件）")
        if report.issues:
            print()
            print(report.render())
        # --dry-run --verify 是有用的组合："这次改动 CubeMX 会不会吃掉"可以在**决定
        # 写不写之前**问一次。往返的结果**要进退出码** —— 这个组合的全部用途就是让
        # 调用方（尤其 Agent）不看人读的报告也能判断能不能写，把它吞掉等于白跑十几秒。
        # 这也正是既有契约的原话："只有校验/往返没过才给 2"。
        verdict = None
        if np.verify and changes and not blocking:
            verdict = _verify_before_write(project, np, baseline)
        if blocking:
            return EXIT_INVALID
        return EXIT_OK if verdict is None else verdict

    if blocking and not np.force:
        print(file=sys.stderr)
        print(report.render(), file=sys.stderr)
        print(
            "\n校验未通过，没有写入文件。（确认无误可用 --force 强制写入）",
            file=sys.stderr,
        )
        return EXIT_INVALID

    if report.issues:
        print(report.render(), file=sys.stderr)
        if not blocking:
            # 剩下的都是"还没配完"，逐字段命令可以分步来 —— 说清楚，别让人以为写坏了
            print(
                "\n以上是尚未配完的部分（逐字段编辑允许分步进行）。"
                "配完后再跑一次 cubemx-cli validate 复核。",
                file=sys.stderr,
            )

    if not changes:
        # 空改动不必验证：没写任何键，往返能查的东西也就没有了。
        print("没有可做的改动。")
        return EXIT_OK

    if np.verify:
        verdict = _verify_before_write(project, np, baseline)
        if verdict is not None:
            return verdict

    saved = project.save(path, backup=not np.no_backup)
    print(f"已写入 {saved}")
    return EXIT_OK


def _print_diff(before: str, after: str) -> None:
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="修改前", tofile="修改后", lineterm="", n=1,
    )
    text = "\n".join(diff)
    print(text if text else "（内容没有变化）")


# ------------------------------------------------------------------ 参数解析


def _parse_kv(raw: list[str] | None) -> dict[str, str]:
    """``["BaudRate=115200"]`` -> ``{"BaudRate": "115200"}``。"""
    out: dict[str, str] = {}
    for item in raw or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise CliError(f"参数要写成 名=值，收到的是 {item!r}")
        out[key.strip()] = value.strip()
    return out


def _signal_pins(unknown: list[str]) -> dict[str, str]:
    """从多余的长选项里抽出「信号短名 -> 引脚」。

    ``add-peripheral proj.ioc USART2 --tx PA2 --rx PA3`` 里的 ``--tx PA2``
    会落到 argparse 的 unknown 里，这里按 ``--<信号名>`` 捡回来。

    **收不下的东西一律报错**，不静默丢弃 —— ``--rr PA4`` 少打一个字母就白跑一趟，
    比报错难查得多。信号名本身是否被该模式需要，由 `ops.add_peripheral` 判。
    """
    out: dict[str, str] = {}
    i = 0
    while i < len(unknown):
        item = unknown[i]
        if not item.startswith("--"):
            raise CliError(f"无法识别的参数 {item!r}（信号要写成 --<信号名> <引脚>，如 --tx PA2）")
        name, sep, value = item[2:].partition("=")
        if sep:
            if not value:
                raise CliError(f"{item} 没有给出引脚名")
            out[name.replace("-", "_").upper()] = value
            i += 1
            continue
        if i + 1 >= len(unknown) or unknown[i + 1].startswith("--"):
            raise CliError(f"{item} 后面没有给引脚名")
        out[name.replace("-", "_").upper()] = unknown[i + 1]
        i += 2
    return out


# ------------------------------------------------------------------ 读命令


def cmd_info(np: argparse.Namespace) -> int:
    project = _open(Path(np.project))
    _device(project)
    pins = project.pins()
    used = [p for p in pins if p.signal]
    if np.json:
        print(json.dumps(
            {
                "file": str(project.source),
                "mcu_name": project.mcu_name,
                "cpn": project.cpn,
                "package": project.package,
                "pins_configured": len(used),
                "pins_total": len(pins),
                "peripherals": list(project.peripheral_names()),
            },
            ensure_ascii=False, indent=2,
        ))
        return EXIT_OK
    print(f"文件      {project.source}")
    print(f"MCU       {project.mcu_name}   ({project.cpn or '?'})")
    print(f"封装      {project.package or '?'}")
    print(f"引脚      {len(used)} 已配置 / {len(pins)} 在清单里")
    print(f"外设      {', '.join(project.peripheral_names()) or '（无）'}")
    return EXIT_OK


def _pins_payload(project: IocProject) -> list[dict]:
    return [p.as_dict() for p in project.pins() if p.signal or p.mode]


def cmd_pins(np: argparse.Namespace) -> int:
    project = _open(Path(np.project))
    _device(project)
    rows = _pins_payload(project)
    if np.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not rows:
        print("（没有已配置的引脚）")
        return EXIT_OK
    for row in rows:
        bits = [f"{row['name']:<20}", f"{row['signal'] or '-':<20}", row["mode"] or "-"]
        if row["label"]:
            bits.append(f"标签={row['label']}")
        if row["properties"]:
            bits.append(" ".join(f"{k}={v}" for k, v in sorted(row["properties"].items())))
        print("  ".join(bits))
    return EXIT_OK


def cmd_peripherals(np: argparse.Namespace) -> int:
    project = _open(Path(np.project))
    _device(project)
    rows = []
    for per in project.peripherals():
        pins = [p.name for p in project.pins() if project.owner_of(p) == per.name]
        rows.append({**per.as_dict(), "pins": pins})
    if np.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    for row in rows:
        virt = " ".join(f"{k}={v}" for k, v in row["virtual"].items())
        print(f"  {row['name']:<10} {virt:<22} 引脚：{', '.join(row['pins']) or '（无）'}")
    return EXIT_OK


def cmd_modes(np: argparse.Namespace) -> int:
    project = _open(Path(np.project))
    _device(project)
    rows = ops.list_modes(project, np.ip)
    if np.json:
        # 文本形态的 virt 是拼好的 `VirtualMode=VM_ASYNC`（给人读）。JSON 里拆成
        # 键和值两段：调用方要拿 `VM_ASYNC` 去喂 --virtual-mode，把整串抄过去只会
        # 得到"参数不认识"；virtual_key 顺带说明 SPI 该用 --virtual-type。
        out = []
        for name, virt, sigs in rows:
            key, _, value = (virt or "").partition("=")
            out.append({
                "name": name,
                "virtual": value or None,
                "virtual_key": key or None,
                "signals": list(sigs),
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return EXIT_OK
    for name, virt, sigs in rows:
        print(f"  {name:<32} {virt or '-':<20} 需要 {', '.join(sigs)}")
    return EXIT_OK


# ------------------------------------------------------------- list（只读浏览）

#: 每一页的标题。`--page` 的取值和这里一一对应。
_PAGES = {
    "mode": "模式",
    "param": "参数",
    "nvic": "中断 NVIC",
    "dma": "DMA 请求",
    "gpio": "引脚 GPIO",
}


def _kv(pairs: tuple[tuple[str, str], ...]) -> str:
    return " ".join(f"{k}={v}" for k, v in pairs) if pairs else "（无）"


def _list_all(project: IocProject, as_json: bool) -> int:
    data = ops.list_instances(project)
    if as_json:
        print(json.dumps({
            "mcu": data.mcu,
            "complete": data.complete,
            "configured": [
                {
                    "name": i.name,
                    "pins": list(i.pins),
                    "params": i.params,
                    "irqs": i.irqs,
                    "dma": i.dma,
                    "virtual": ({"key": i.virtual[0], "value": i.virtual[1]}
                                if i.virtual else None),
                }
                for i in data.configured
            ],
            "unconfigured": [i.name for i in data.others],
        }, ensure_ascii=False, indent=2))
        return EXIT_OK

    note = "" if data.complete else "（缺数据库，这里只有工程里已声明过的外设）"
    print(f"{data.mcu}  {len(data.instances)} 个外设，已配置 {len(data.configured)} 个{note}")
    print("\n已配置")
    if not data.configured:
        print("  （无）")
    for i in data.configured:
        bits = []
        if i.virtual:
            bits.append(f"{i.virtual[0]}={i.virtual[1]}")
        if i.pins:
            bits.append(f"引脚 {', '.join(i.pins)}")
        for label, n in (("参数", i.params), ("中断", i.irqs), ("DMA", i.dma)):
            if n:
                bits.append(f"{label} {n}")
        print(f"  {i.name:<12} {'  '.join(bits)}")
    print("\n未配置")
    names = [i.name for i in data.others]
    if not names:
        print("  （无）")
    else:
        # 挤成几行：几十个外设每个占一行没法扫，这里按宽度折行
        line = "  "
        for n in names:
            if len(line) + len(n) + 2 > 78:
                print(line)
                line = "  "
            line += n + "  "
        if line.strip():
            print(line)
    return EXIT_OK


def _render_modes(result: ops.InspectResult) -> None:
    print("\n[模式]")
    for m in result.modes:
        virt = f"{m.virtual_key}={m.virtual}" if m.virtual else "-"
        mark = "   ← 当前" if m.current else ""
        need = ", ".join(m.signals) if m.signals else "（无信号需求）"
        print(f"  {m.name:<34} {virt:<20} 需要 {need}{mark}")


def _render_params(result: ops.InspectResult, verbose: bool) -> None:
    print("\n[参数]")
    for p in result.params:
        if p.set:
            origin = "工程里已设"
        else:
            origin = "库里默认" if p.value else "库里没给默认值"
        line = f"  {p.name:<24} = {p.value or '（无）':<22} （{origin}）"
        print(line)
        if verbose and p.values:
            print(f"      可选：{', '.join(p.values)}")


def _render_nvic(nvic: ops.NvicPage) -> None:
    lo, hi = nvic.preemption
    slo, shi = nvic.sub
    print(f"\n[中断 NVIC]  分组 {nvic.priority_group}（抢占 {lo}-{hi}，子优先级 {slo}-{shi}）")
    for i in nvic.irqs:
        where = "本外设" if i.owned else f"随 DMA 请求 {i.from_dma}"
        state = (f"已启用  抢占 {i.preemption} 子 {i.sub}" if i.enabled else "未启用")
        cond = f"  条件 {i.condition}" if i.condition else ""
        print(f"  {i.name:<24} {i.comment or '':<40} {state}   （{where}）{cond}")
    free = nvic.settable
    print(f"  还可以开：{', '.join(i.name for i in free) if free else '无'}")


def _render_dma(rows: tuple[ops.DmaRow, ...]) -> None:
    print("\n[DMA 请求]")
    if not rows:
        print("  （本外设没有 DMA 请求）")
        return
    for r in rows:
        cur = f"已配 {r.current}" if r.current else "未配"
        print(f"  {r.request:<18} {cur:<20} 可选 stream：{', '.join(r.streams) or '无'}")
        if r.fixed:
            print("      " + "  ".join(f"{k}={v}（固定）" for k, v in r.fixed))
        # 固定项上面那行已经带了，这里只报工程里实际设过的可调参数
        fixed_names = {k for k, _ in r.fixed}
        rest = tuple((k, v) for k, v in r.params if k not in fixed_names)
        if rest:
            print("      " + _kv(rest))
        elif r.settable:
            print(f"      可设：{', '.join(r.settable)}")


def _render_pins(rows: tuple[ops.PinRow, ...]) -> None:
    print("\n[引脚 GPIO]")
    if not rows:
        print("  （本外设没有占用引脚）")
        return
    for p in rows:
        label = f"  Label={p.label}" if p.label else ""
        print(f"  {p.pin:<8} Signal={p.signal or '-':<16} Mode={p.mode or '-':<14}{label}")
        print(f"      现在：{_kv(p.params)}")
        print(f"      可设：{', '.join(p.settable)}")


#: JSON 里的页名 -> `--page` 的取值。两套名字不一样（`modes` 对 `mode`）。
_PAGE_KEYS = {"modes": "mode", "params": "param", "nvic": "nvic", "dma": "dma", "pins": "gpio"}

#: 页名 -> "看不了"那几个字里的人话。
_PAGE_LABEL = {
    "mode": "模式库", "param": "参数表", "nvic": "中断表",
    "dma": "DMA 请求表", "gpio": "引脚 GPIO",
}


def _page(
    key: str, value, *, page: str | None, missing: tuple[str, ...]
):
    """一页的数据；**没有结论就给 ``None``**。

    ``null`` = 没问（``--page`` 没点它）或者问了但读不出来（`missing` 里点名）；
    ``[]`` = 问了，确实是空的。两者绝不能混 —— 把"没查"渲染成空列表，AI 会
    转述成"这个外设没有中断"，而那是一句**错误的事实**。
    """
    if not (page is None or page == _PAGE_KEYS[key]) or _PAGE_KEYS[key] in missing:
        return None
    return value


def _list_one(project: IocProject, ip: str, page: str | None, as_json: bool) -> int:
    result = ops.inspect(project, ip, page)

    if as_json:
        print(json.dumps({
            "ip": result.ip,
            "kind": result.kind,
            "version": result.version,
            "configured": result.configured,
            "mode": result.mode,
            "virtual": ({"key": result.virtual[0], "value": result.virtual[1]}
                        if result.virtual else None),
            "missing": list(result.missing),
            "modes": _page("modes", [
                {"name": m.name, "virtual": m.virtual, "virtual_key": m.virtual_key,
                 "signals": list(m.signals), "current": m.current}
                for m in result.modes
            ], page=page, missing=result.missing),
            "params": _page("params", [
                {"name": p.name, "value": p.value, "set": p.set, "default": p.default,
                 "values": list(p.values), "comment": p.comment}
                for p in result.params
            ], page=page, missing=result.missing),
            "nvic": None if result.nvic is None else {
                "priority_group": result.nvic.priority_group,
                "preemption": list(result.nvic.preemption),
                "sub": list(result.nvic.sub),
                "irqs": [
                    {"name": i.name, "comment": i.comment, "enabled": i.enabled,
                     "preemption": i.preemption, "sub": i.sub, "owned": i.owned,
                     "from_dma": i.from_dma, "condition": i.condition}
                    for i in result.nvic.irqs
                ],
            },
            "dma": _page("dma", [
                {"request": r.request, "streams": list(r.streams), "current": r.current,
                 "fixed": dict(r.fixed), "params": dict(r.params),
                 "settable": list(r.settable)}
                for r in result.dma
            ], page=page, missing=result.missing),
            "pins": _page("pins", [
                {"pin": p.pin, "signal": p.signal, "mode": p.mode, "label": p.label,
                 "params": dict(p.params), "settable": list(p.settable)}
                for p in result.pins
            ], page=page, missing=result.missing),
        }, ensure_ascii=False, indent=2))
        return EXIT_OK

    head = f"{result.ip}"
    if result.kind:
        head += f"  ({result.kind} {result.version})"
    if result.configured:
        if result.mode:
            head += f"  模式 {result.mode}"
        if result.virtual:
            head += f"  {result.virtual[0]}={result.virtual[1]}"
        if result.pins:
            head += f"  引脚 {', '.join(p.pin for p in result.pins)}"
    else:
        head += "  （未配置）"
    print(head)

    # 没给 --page 时全给，但参数页只列名字和值；要取值清单就明说 --page param。
    # `result.has(...)` 是必须的：读不出来的页要**整页不印**，不能印成
    # "（本外设没有 DMA 请求）" —— 那是把"没查"说成了一个结论。
    verbose = page == "param"
    if page in (None, "mode") and result.has("mode"):
        _render_modes(result)
    if page in (None, "param") and result.has("param"):
        _render_params(result, verbose)
    if page in (None, "nvic") and result.has("nvic"):
        _render_nvic(result.nvic)
    if page in (None, "dma") and result.has("dma"):
        _render_dma(result.dma)
    if page in (None, "gpio") and result.has("gpio"):
        _render_pins(result.pins)

    if result.missing:
        labels = "、".join(_PAGE_LABEL.get(p, p) for p in result.missing)
        print(f"\n看不了：{labels}（数据库里没有这份表，或格式不认识）{_PAGE_HINT}")
    return EXIT_OK


#: 缺数据时的补救提示。和 `validate` 的 `skipped` 一样：**"没查"不等于"查了没问题"**。
_PAGE_HINT = " —— 先跑 cubemx-cli config 确认数据库路径。"


def cmd_list(np: argparse.Namespace) -> int:
    project = _open(Path(np.project))
    _device(project)
    if np.ip is None:
        if np.page:
            raise CliError("--page 要指定外设才有意义：list <工程> <外设> --page nvic")
        return _list_all(project, np.json)
    return _list_one(project, np.ip, np.page, np.json)


def cmd_validate(np: argparse.Namespace) -> int:
    project = _open(Path(np.project))
    _device(project)
    report = validate.validate(project)
    if np.json:
        print(json.dumps(
            {
                "ok": report.ok,
                "issues": [i.__dict__ for i in report.issues],
                "skipped": report.skipped,
            },
            ensure_ascii=False, indent=2,
        ))
        return EXIT_OK if report.ok else EXIT_INVALID
    print(report.render())
    return EXIT_OK if report.ok else EXIT_INVALID


def cmd_show(np: argparse.Namespace) -> int:
    print(_open(Path(np.project)).render(), end="")
    return EXIT_OK


# ------------------------------------------------------------------ 配置


#: `resolve_db_root()` 的 source 是给程序看的短标识，这里是给人看的。
_SOURCE_LABEL = {
    "env": "环境变量",
    "config": "配置文件",
    "auto": "自动探测",
    "none": "没找到",
}


def _config_payload() -> dict:
    """本次运行的配置全貌 —— 文本视图和 ``--json`` 共用一份数据，不各算各的。"""
    res = config.resolve_db_root()
    path = settings_mod.config_path()
    exe = config.cube_mx_exe()
    return {
        "config_file": str(path),
        "config_file_exists": path.exists(),
        "db_root": str(res.path) if res.path else None,
        "source": res.source,
        "detail": res.detail,
        "cube_mx_exe": str(exe) if exe else None,
        "problems": list(res.problems),
    }


def _config_show(as_json: bool) -> int:
    data = _config_payload()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK

    exists = "存在" if data["config_file_exists"] else "还没有"
    label = _SOURCE_LABEL.get(data["source"], data["source"])
    detail = f" —— {data['detail']}" if data["detail"] else ""
    print(f"配置文件  {data['config_file']}（{exists}）")
    print(f"数据库    {data['db_root'] or '（没找到）'}")
    print(f"来源      {label}{detail}")
    print(f"CubeMX    {data['cube_mx_exe'] or '（没找到）'}")
    if data["problems"]:
        print("\n问题：")
        for p in data["problems"]:
            print(f"  - {p}")
    return EXIT_OK


def _write_config(cfg: settings_mod.Settings) -> None:
    """`config` 命令专用的落盘 —— 这里**存不下来就是失败**。

    别的命令相反（见 `_save_settings`）：用户跑 `config --set-db` 就是来存这个的，
    存不下还报成功等于骗人。
    """
    target = settings_mod.config_path()
    try:
        settings_mod.save(cfg)
    except OSError as exc:
        raise CliError(f"配置写不进去（{target}）：{exc}") from exc


def _confirm_config(saved: settings_mod.Settings, as_json: bool) -> None:
    """存完把**本次实际生效的**来源再报一次。

    存进去 ≠ 生效 —— `ST_CUBEMX_DB` 优先级更高，用户存完发现没变化，得有句话告诉他
    为什么，否则会以为工具坏了。
    """
    if as_json:
        print(json.dumps(_config_payload(), ensure_ascii=False, indent=2))
        return
    if saved.db_root:
        print(f"已保存  db_root     = {saved.db_root}")
    if saved.cube_mx_exe:
        print(f"        cube_mx_exe = {saved.cube_mx_exe}")

    res = config.resolve_db_root()
    label = _SOURCE_LABEL.get(res.source, res.source)
    print(f"当前生效  {label} —— {res.path or '（没找到）'}")
    if res.source != "config":
        print(f"注意：现在生效的是{label}，不是刚存下的配置文件 —— 环境变量的优先级更高。")


def cmd_config(np: argparse.Namespace) -> int:
    if np.set_db:
        return _config_set_db(np.set_db, np.json)
    if np.set_cube_mx:
        return _config_set_cube_mx(np.set_cube_mx, np.json)
    return _config_show(np.json)


def _config_set_db(raw: str, as_json: bool) -> int:
    got = settings_mod.find_db_root(Path(raw))
    if got is None:
        raise CliError(
            f"{raw} 里没找到 db\\mcu（顶层要有 STM32*.xml）。"
            "可以直接给 CubeMX 的安装目录，我们往下替你找一层。"
        )
    _write_config(settings_mod.Settings(db_root=got))
    _confirm_config(settings_mod.Settings(db_root=got), as_json)
    return EXIT_OK


def _install_exe(*candidates: Path) -> Path | None:
    """在候选目录里找 CubeMX 可执行文件。找不到就返回 None（不是错误）。

    两个候选：用户给的目录本身，以及从 ``db/mcu`` 往上退两层 —— 这样无论他给的是
    安装目录还是数据库目录，都还能把 exe 记下来。
    """
    for start in candidates:
        for name in ("STM32CubeMX.exe", "STM32CubeMX"):
            found = start / name
            if found.is_file():
                return found
    return None


def _config_set_cube_mx(raw: str, as_json: bool) -> int:
    base = Path(raw)
    if not base.is_dir():
        raise CliError(f"{base} 不是一个存在的目录。")
    db = settings_mod.find_db_root(base)
    if db is None:
        raise CliError(f"{base} 里没找到 db\\mcu（顶层要有 STM32*.xml）。")

    cfg = settings_mod.Settings(
        db_root=db, cube_mx_exe=_install_exe(base, db.parent.parent)
    )
    _write_config(cfg)
    _confirm_config(cfg, as_json)
    return EXIT_OK


# ------------------------------------------------------------------ 首次运行


def _is_interactive() -> bool:
    """stdin 是不是真的在等一个人敲键盘。

    **不能只看 ``sys.stdin.isatty()``** —— Windows 上 ``NUL`` 也是一个"字符设备"，
    于是 ``< NUL``（计划任务、服务宿主、不少 spawn API 给的都近似这个）会让它返回
    True。实测过：那样我们会对着一个立刻 EOF 的句柄把提示语打出来，然后报一句
    "本次不启用数据库校验"，**而数据库其实是自动探测到了的** —— 又吵又说谎。

    所以 Windows 上额外要求这个句柄有控制台模式（``GetConsoleMode`` 只有真控制台
    才有；``NUL`` 没有）。判错的两个方向代价差得很远：判成"能问"而其实不能，是往
    空句柄写提示；判成"不能问"只是少问一句。
    """
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return False
    except (AttributeError, ValueError, OSError):
        return False

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            mode = ctypes.c_uint32()
            # -10 = STD_INPUT_HANDLE
            return bool(
                kernel32.GetConsoleMode(kernel32.GetStdHandle(-10), ctypes.byref(mode))
            )
        except Exception:  # noqa: BLE001 - 拿不到就当没有终端
            return False
    return True


def _ask(prompt: str) -> str | None:
    """问一行，EOF / Ctrl-C 都返回 None（都该当成"跳过"，不是崩溃）。

    **不用 ``input()``**：它会往 stdout 写提示语，而 stdout 是 ``--json`` 的载荷通道。
    """
    print(prompt, end="", file=sys.stderr, flush=True)
    try:
        line = sys.stdin.readline()
    except (KeyboardInterrupt, OSError):
        return None
    return line.strip() if line else None


def _save_settings(cfg: settings_mod.Settings) -> None:
    """存配置，**存不下不算失败**。

    ``%APPDATA%`` 可能是只读的（企业策略、受限账户）。为了"记住一个路径"而让整条
    命令挂掉，代价和收益完全不成比例 —— 把这个值用在本次运行上就行。
    """
    try:
        settings_mod.save(cfg)
    except OSError as exc:
        print(f"警告：配置存不下来（{exc}），这次先用着。", file=sys.stderr)


def _prompt_for_db(default: Path | None) -> Path | None:
    """问出 ``db/mcu`` 的位置。返回 None 表示用户跳过。"""
    print("还没有配置 CubeMX 的位置，先配一次（以后不再问）。", file=sys.stderr)
    print("（MCU 数据库是引脚能力、模式、参数这三层校验的依据）", file=sys.stderr)
    if default is not None:
        print(f"直接回车就用自动探测到的：{default}", file=sys.stderr)

    for _ in range(3):
        hint = "CubeMX 安装目录（或它下面的 db\\mcu）"
        if default is not None:
            hint += f" [{default}]"
        line = _ask(hint + "\n> ")

        if line is None:  # EOF / Ctrl-C
            return None
        if not line:  # 空输入：有默认值就认，没有就是跳过
            return default

        found = settings_mod.find_db_root(Path(line.strip("\"'").strip()))
        if found is not None:
            return found
        print(
            f"  {line} 里没找到 db\\mcu（顶层要有 STM32*.xml）。"
            "可以直接给 CubeMX 的安装目录。",
            file=sys.stderr,
        )

    print("连续 3 次都没认出来，这次先跳过。", file=sys.stderr)
    return None


def _maybe_first_run(no_input: bool = False) -> None:
    """第一次运行（配置文件还不存在）时问一次数据库在哪。

    **只在真终端里问。** 管道、重定向、Agent 调用时 stdin 不是终端，弹提示会永久
    阻塞在读取上 —— 那是"首次必须配一次"唯一能造成的实际伤害。非交互时就吃自动探测
    的结果，只说一句怎么把它固定下来。

    "配过了"的判据就是**配置文件存在**，不另设标志位 —— 两个状态早晚会不同步。
    """
    if no_input or settings_mod.config_path().exists():
        return

    if not _is_interactive():
        res = config.resolve_db_root()
        if res.path is not None:
            print(
                f"提示：配置还没固定，用的是自动探测到的 {res.path}。"
                f'跑 cubemx-cli config --set-db "{res.path}" 可以固定下来。',
                file=sys.stderr,
            )
        return

    chosen = _prompt_for_db(config.resolve_db_root().path)
    if chosen is None:
        # 只说"没配上"，**不断言数据库没启用** —— 自动探测可能仍然找得到，说数据库
        # 的事是 `_report_db` 的活。两处都断言就会互相打架。
        print("这次没配置；随时可以跑 cubemx-cli config --set-db <目录> 补上。", file=sys.stderr)
        return
    _save_settings(settings_mod.Settings(db_root=chosen))
    print(f"已记住 {chosen}", file=sys.stderr)


def _report_db(res: config.DbResolution) -> None:
    """数据库没着落时说清楚**为什么**。

    早先是笼统一句"请设 ST_CUBEMX_DB"。对**已经设了但设错**的人，那句话只会把他往
    错的方向再推一次 —— 他设的是 ``ST_CUBEMX_EXE``，你再让他去设 ``ST_CUBEMX_DB``，
    他怎么设都不会好。所以现在有几条 problem 就报几条，点名到变量/文件。
    """
    if res.path is not None:
        for p in res.problems:
            print(f"提示：{p}", file=sys.stderr)
        return

    print(
        "提示：没有找到 CubeMX 数据库。跑 cubemx-cli config --set-db "
        r"<CubeMX 安装目录> 固定下来，或设 ST_CUBEMX_DB 指向 <STM32CubeMX>\db\mcu。",
        file=sys.stderr,
    )
    for p in res.problems:
        print(f"      {p}", file=sys.stderr)
    print(
        "      没有它，引脚能力、模式、参数这三层校验会跳过；意图级命令不可用。",
        file=sys.stderr,
    )


# ------------------------------------------------------------------ 写命令


def cmd_set_pin(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    props = _parse_kv(np.set)
    return _apply(
        project, path,
        lambda: ops.set_pin(
            project, np.pin,
            signal=np.signal, mode=np.mode, label=np.label,
            locked=(True if np.lock else (False if np.unlock else None)),
            props=props, force=np.force,
        ),
        np,
    )


def cmd_clear_pin(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    return _apply(project, path, lambda: ops.clear_pin(project, np.pin), np)


def cmd_set_peripheral(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    params = _parse_kv(np.param)

    def mutate() -> list[str]:
        changes: list[str] = []
        virtual = None
        if np.virtual_mode:
            virtual = ("VirtualMode", np.virtual_mode)
        elif np.virtual_type:
            virtual = ("VirtualType", np.virtual_type)

        # "参数给没给"必须在**改动之前**判。原先这句放在收尾处判 `if not changes`，
        # 于是"给了 --param，值恰好和现在一样"会被报成"没有给出要改什么" ——
        # 那是在骂用户没给参数，而他给了。空改动本来就该按空改动处理：
        # 交给 _apply 打"没有可做的改动。"（退出码 0，也不写盘、不留备份）。
        if not (virtual or params or np.unset):
            raise ops.OpError("没有给出要改什么（--virtual-mode / --param / --unset）")

        if virtual or params:
            changes += ops.set_peripheral(project, np.ip, virtual=virtual, params=params)
        for key in np.unset or []:
            per = project.find_peripheral(np.ip)
            if per is None:
                raise ops.OpError(f"工程里没有外设 {np.ip}")
            if per.get_param(key) is None:
                # 本来就没有这个键。报成"已删除"是谎报改动 —— 而且会让 _apply
                # 白写一次盘、白留一份备份。
                continue
            per.set_param(key, None)
            changes.append(f"{np.ip}.{key} 已删除")
        return changes

    return _apply(project, path, mutate, np)


def cmd_remove_peripheral(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    return _apply(
        project, path,
        lambda: ops.remove_peripheral(project, np.ip, clear_pins=not np.keep_pins),
        np,
    )


def cmd_add_peripheral(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    pins = np.pins
    params = _parse_kv(np.param)
    mode, force = np.mode, np.force

    def mutate() -> list[str]:
        result = ops.add_peripheral(
            project, np.ip, mode=mode, pins=pins, params=params, force=force,
        )
        lines = [f"模式 {result.mode}"]
        for a in result.allocations:
            lines.append(f"  {a}")
        return lines + result.changes

    return _apply(project, path, mutate, np)


def cmd_add_gpio(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    props = _parse_kv(np.set)
    return _apply(
        project, path,
        lambda: ops.add_gpio(
            project, np.pin, mode=np.mode, label=np.label,
            locked=not np.unlock, props=props,
        ),
        np,
    )


# ------------------------------------------------------------------ 中断 / DMA


def cmd_enable_irq(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    return _apply(
        project, path,
        lambda: ops.enable_irq(project, np.irq, preemption=np.preemption, sub=np.sub),
        np,
    )


def cmd_disable_irq(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    return _apply(project, path, lambda: ops.disable_irq(project, np.irq), np)


def cmd_add_dma(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    params = _parse_kv(np.param)
    return _apply(
        project, path,
        lambda: ops.add_dma(
            project, np.request, stream=np.stream, params=params,
            irq=not np.no_irq, preemption=np.preemption,
        ),
        np,
    )


def cmd_remove_dma(np: argparse.Namespace) -> int:
    path = Path(np.project)
    project = _open(path)
    _device(project)
    return _apply(
        project, path,
        lambda: ops.remove_dma(project, np.request, irq=not np.keep_irq),
        np,
    )


def cmd_verify(np: argparse.Namespace) -> int:
    """只读：让 CubeMX 走一次往返，看它认不认**现在**这份工程。

    和写命令上的 ``--verify`` 共用 `cubemx.verify`，区别只在没有 baseline ——
    这里问的是"这份工程 CubeMX 收不收"，所以任何键丢了都算数。
    不挂数据库：往返验证只用文件文本，没有数据库也照样跑得起来。
    """
    path = Path(np.project)
    project = _open(path)

    result = cubemx.verify(
        project,
        exe=config.cube_mx_exe(),
        timeout=np.verify_timeout,
        keep=np.keep_temp,
    )
    if not result.ran:
        if np.json:
            print(json.dumps(
                {"ran": False, "ok": False, "failure": result.failure},
                ensure_ascii=False, indent=2,
            ))
        else:
            print(f"往返验证没跑成：{result.failure}", file=sys.stderr)
            if result.detail:
                print(result.detail, file=sys.stderr)
        return EXIT_USAGE

    if np.json:
        print(json.dumps(
            {"ran": True, **result.round_trip.to_dict()}, ensure_ascii=False, indent=2,
        ))
    else:
        print(result.round_trip.render())
    return EXIT_OK if result.round_trip.ok else EXIT_INVALID


# ------------------------------------------------------------------ 入口


def _add_verify_flags(p: argparse.ArgumentParser) -> None:
    """往返验证的两个调节项。写命令和只读的 ``verify`` 命令都要。"""
    p.add_argument(
        "--verify-timeout", type=float, default=cubemx.DEFAULT_TIMEOUT, metavar="秒",
        help=f"往返验证的上限秒数（默认 {cubemx.DEFAULT_TIMEOUT:.0f}）",
    )
    p.add_argument(
        "--keep-temp", action="store_true",
        help="留着往返验证的临时目录（报告看不懂时当证据）",
    )


def _add_write_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true",
                   help="只显示改动和 diff，不写文件")
    p.add_argument("--no-backup", action="store_true",
                   help="覆盖前不生成 .bak-<时间戳> 备份")
    p.add_argument("--force", action="store_true",
                   help="校验有错误也照样写入")
    p.add_argument(
        "--verify", action="store_true",
        help="落盘前让 CubeMX 走一次加载-保存往返，确认它接受这次的改动"
             "（起一次 JVM 要十几秒，所以不默认开）",
    )
    _add_verify_flags(p)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cubemx-cli",
        description=(
            "读写 STM32CubeMX 的 .ioc 工程。改动先在内存里做，\n"
            "校验通过才落盘；不通过则原文件一个字节都不变。"
        ),
        # 默认的 HelpFormatter 会把 epilog 里的换行揉成一段 —— 例子挤成一行很难读，
        # 所以这里自己排好版再交给 Raw 版原样输出。
        epilog=(
            "例：\n"
            "  cubemx-cli add-peripheral proj.ioc USART2 --mode Asynchronous "
            "--tx PA2 --rx PA3\n"
            "  cubemx-cli enable-irq proj.ioc USART2_IRQn --preemption 5\n"
            "  cubemx-cli add-dma proj.ioc USART2_RX --verify\n"
            "  cubemx-cli config --set-db D:\\stm32CubeMX\n"
            "\n"
            "中断名 / 请求名可以直接从 `list <工程> <外设> --page nvic|dma` "
            "的输出里抄。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-input", action="store_true",
        help="不询问任何问题（脚本、Agent 用）",
    )
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    for name, fn, help_ in (
        ("info", cmd_info, "工程摘要"),
        ("pins", cmd_pins, "列出已配置的引脚"),
        ("peripherals", cmd_peripherals, "列出外设及其引脚"),
        ("validate", cmd_validate, "跑全部校验"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("project")
        p.add_argument("--json", action="store_true", help="输出 JSON")
        p.set_defaults(func=fn)

    p = sub.add_parser("modes", help="列出某外设可选的模式")
    p.add_argument("project")
    p.add_argument("ip")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_modes)

    p = sub.add_parser(
        "list",
        help="浏览可配置项：不给外设列全部外设，给了就列它的配置页（只读）",
    )
    p.add_argument("project")
    p.add_argument("ip", nargs="?", help="外设实例名，如 USART1；不给就列全部外设")
    p.add_argument(
        "--page", choices=sorted(_PAGES),
        help="只看某一页：" + " / ".join(f"{k}={v}" for k, v in _PAGES.items()),
    )
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="把工程原样打印到标准输出")
    p.add_argument("project")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("config", help="查看/保存 CubeMX 安装位置")
    p.add_argument("--set-db", metavar="目录",
                   help=r"保存数据库目录；给 CubeMX 安装目录也行，会自动归一化到 db\mcu")
    p.add_argument("--set-cube-mx", metavar="目录",
                   help=r"保存 CubeMX 安装目录，并推导出它旁边的 db\mcu")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("set-pin", help="配置单个引脚（逐字段）")
    p.add_argument("project")
    p.add_argument("pin")
    p.add_argument("--signal", help="如 USART2_TX / GPIO_Output / GPXTI0")
    p.add_argument("--mode", help="取值来自模式库，如 Asynchronous")
    p.add_argument("--label", help="GPIO_Label（会自动登记进 GPIOParameters）")
    p.add_argument("--set", action="append", metavar="键=值",
                   help="其余引脚属性，如 --set GPIO_PuPd=GPIO_NOPULL（可重复）")
    p.add_argument("--lock", action="store_true")
    p.add_argument("--unlock", action="store_true")
    _add_write_flags(p)
    p.set_defaults(func=cmd_set_pin)

    p = sub.add_parser("clear-pin", help="清掉引脚的配置")
    p.add_argument("project")
    p.add_argument("pin")
    _add_write_flags(p)
    p.set_defaults(func=cmd_clear_pin)

    p = sub.add_parser("set-peripheral", help="配置外设参数（逐字段）")
    p.add_argument("project")
    p.add_argument("ip")
    p.add_argument("--virtual-mode", metavar="VM_ASYNC",
                   help="USART/UART/I2S/SAI/USB 的外设级模式键")
    p.add_argument("--virtual-type", metavar="VM_MASTER",
                   help="SPI 专用（它不用 VirtualMode）")
    p.add_argument("--param", action="append", metavar="键=值",
                   help="如 --param BaudRate=115200（可重复，自动登记进 IPParameters）")
    p.add_argument("--unset", action="append", metavar="键", help="删除某参数（可重复）")
    _add_write_flags(p)
    p.set_defaults(func=cmd_set_peripheral)

    p = sub.add_parser("remove-peripheral", help="移除外设")
    p.add_argument("project")
    p.add_argument("ip")
    p.add_argument("--keep-pins", action="store_true",
                   help="保留引脚，只清掉它们的 Signal/Mode")
    _add_write_flags(p)
    p.set_defaults(func=cmd_remove_peripheral)

    p = sub.add_parser(
        "add-peripheral",
        help="一条命令配好一个外设（自动挑引脚、定模式、写全派生键）",
    )
    p.add_argument("project")
    p.add_argument("ip", help="外设实例名，如 USART2 —— 必须是这颗 MCU 上真实存在的")
    p.add_argument("--mode", help="不给就按模式库的默认值选（USART → Asynchronous）")
    p.add_argument("--param", action="append", metavar="键=值", help="可重复")
    _add_write_flags(p)
    p.set_defaults(func=cmd_add_peripheral, pins={})

    p = sub.add_parser("add-gpio", help="把引脚配成 GPIO")
    p.add_argument("project")
    p.add_argument("pin")
    p.add_argument("--mode", required=True, choices=["Input", "Output", "Analog", "EXTI"])
    p.add_argument("--label", help="会自动登记进 GPIOParameters")
    p.add_argument("--set", action="append", metavar="键=值",
                   help="如 --set GPIO_PuPd=GPIO_NOPULL（可重复）")
    p.add_argument("--unlock", action="store_true", help="不写 Locked=true")
    _add_write_flags(p)
    p.set_defaults(func=cmd_add_gpio)

    # 中断 / DMA 这两组**只收一个名字**（IRQ 名或请求名），不收 `<外设>`：
    # 名字在这颗 MCU 上唯一，而且 `DMA2_Stream2_IRQn` 并不"属于" USART1 ——
    # 多一个参数只会多一种错法。名字直接取自
    # `list <工程> <外设> --page nvic|dma`，可以原样粘贴。
    p = sub.add_parser("enable-irq", help="开启一个中断（已开着就只改优先级）")
    p.add_argument("project")
    p.add_argument("irq", metavar="中断名",
                   help="带 _IRQn 后缀，如 USART1_IRQn / DMA2_Stream2_IRQn")
    p.add_argument("--preemption", type=int, metavar="N", help="抢占优先级；不给就沿用现值")
    p.add_argument("--sub", type=int, metavar="N", help="子优先级；不给就沿用现值")
    _add_write_flags(p)
    p.set_defaults(func=cmd_enable_irq)

    p = sub.add_parser("disable-irq", help="关闭一个中断（= 删掉那一行）")
    p.add_argument("project")
    p.add_argument("irq", metavar="中断名", help="带 _IRQn 后缀，如 USART1_IRQn")
    _add_write_flags(p)
    p.set_defaults(func=cmd_disable_irq)

    p = sub.add_parser("add-dma", help="给一个 DMA 请求挂上通道")
    p.add_argument("project")
    p.add_argument("request", metavar="请求名", help="如 USART1_RX / SPI1_TX")
    p.add_argument("--stream", metavar="DMA2_Stream2",
                   help="不给就自动挑第一个没被占用的，并打印出来")
    p.add_argument("--param", action="append", metavar="键=值",
                   help="如 --param Mode=DMA_CIRCULAR（可重复）")
    p.add_argument("--no-irq", action="store_true", help="不顺手打开该通道的中断")
    p.add_argument("--preemption", type=int, metavar="N", help="该通道中断的抢占优先级")
    _add_write_flags(p)
    p.set_defaults(func=cmd_add_dma)

    p = sub.add_parser("remove-dma", help="摘掉一个 DMA 请求（后面的请求会整体前移一位）")
    p.add_argument("project")
    p.add_argument("request", metavar="请求名", help="如 USART1_RX")
    p.add_argument("--keep-irq", action="store_true",
                   help="保留释放出来的那个通道中断（默认跟着关掉）")
    _add_write_flags(p)
    p.set_defaults(func=cmd_remove_dma)

    p = sub.add_parser(
        "verify",
        help="让 CubeMX 走一次加载-保存往返，看它认不认这份工程（只读）",
    )
    p.add_argument("project")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    _add_verify_flags(p)
    p.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # add-peripheral 的 --tx/--rx 这类信号选项是动态的（取决于外设），
    # 交给 parse_known_args 收，再自己捡回来。
    try:
        np, unknown = parser.parse_known_args(argv)
    except SystemExit as exc:
        # argparse 自己出错时走的是 `sys.exit(2)`，而 2 在我们的契约里是"工程校验
        # 没过"（Skill 就靠这个区分"改错了"和"命令写错了"）。缺参数、选项值非法
        # 这类**命令行**错误必须归到 EXIT_USAGE，否则 Agent 会把少打一个参数读成
        # "这个工程有问题"，然后去修一个根本没坏的东西。
        # `--help` 走的是 `exit()`（code 为 None），那是成功路径，照原样放行。
        code = exc.code if isinstance(exc.code, int) else 0
        return EXIT_OK if code == 0 else EXIT_USAGE

    # argparse 的全局选项只能写在子命令**前面**，`... info proj.ioc --no-input` 会掉进
    # unknown。这里捞回来：这个选项没有歧义，为它报一次"无法识别的选项"不值当。
    # 必须在 add-peripheral 的 `_signal_pins` 之前捡走，否则它会被当成信号名。
    if "--no-input" in unknown:
        unknown = [a for a in unknown if a != "--no-input"]
        np.no_input = True

    if not getattr(np, "func", None):
        parser.print_help()
        return EXIT_USAGE

    if np.command == "add-peripheral":
        try:
            np.pins = _signal_pins(unknown)
        except CliError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return EXIT_USAGE
    elif unknown:
        print(f"错误：无法识别的选项 {', '.join(unknown)}", file=sys.stderr)
        return EXIT_USAGE

    # `config` 自己跳过这两步：它不碰工程，而且首次提示会让"非交互地配一次"变成死锁
    # —— 提示拦在配置命令前面，就永远没有第一次。
    if np.command != "config":
        _maybe_first_run(np.no_input)
        _report_db(config.resolve_db_root())

    try:
        return np.func(np)
    except CliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except (ops.OpError, ProjectError, DbError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
