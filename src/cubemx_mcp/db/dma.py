"""DMA 请求表：``db/mcu/IP/DMA-<MCU>_Modes.xml``。

一个外设想用 DMA，``.ioc`` 里要写三样东西（样例 ``Dma.USART1_RX.0.*``）：

```
Dma.Request0=USART1_RX              ← 这个名字是"请求名"
Dma.USART1_RX.0.Instance=DMA2_Stream2
Dma.USART1_RX.0.RequestParameters=Instance,Direction,PeriphInc,…
```

这份文件回答两个问题：

1. **这个外设有哪些 DMA 请求？** 每个请求是一个 ``<RefMode>``，里面
   ``<Parameter Name="IpInstance"><PossibleValue>USART1</PossibleValue>`` 就是
   它属于谁。注意这个关联**只在每模式的覆盖里**，不在顶层 ``<RefParameter>`` ——
   所以 `ModeDb.param("IpInstance").values` 是空的，必须自己读。
2. **每个请求能挂到哪些 stream 上？** 藏在 ``<ModeLogicOperator>`` 树里：

   ```
   <ModeLogicOperator Name="OR">
     <Mode Name="DMA1">
       <ModeLogicOperator Name="OR">
         <Mode Name="DMA1_Stream0">
           <ModeLogicOperator Name="XOR">
             <Mode Name="SPI3_RX"><Condition Expression="…"/></Mode>
             <Mode Name="I2C1_RX">…
   ```

   即「每个 stream 下面 XOR 挂着一串能用它的请求」。`db.xml.descendants` 会把父子
   关系拍平，所以这里自己递归走。

**参数表整个复用 `ModeDb`。** 请求的参数（``Priority``/``Mode``/``MemInc`` …）在
``BaseMode="DMA_Request"`` 里声明，而 `ModeDb` 早就在读 `<RefMode>` 的
`<Parameter>` 子元素并沿 `BaseMode` 展开了 —— 这里不再重复实现一遍
``<RefParameter>``/``<PossibleValue>`` 的解析。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .modes import ModeDb, ParamDef
from .xml import DbError, children, descendants, local, parse

#: stream 的判据。**不依赖固定深度** —— 树的层数在不同系列上不一样（F4 是
#: 根/DMA1/stream/请求 四层，别的系列未必）。
STREAM_RE = re.compile(r"^DMA\d+_Stream\d+$")

#: 不是外设请求的内部条目。
NON_REQUEST_MODES = frozenset({"MEMTOMEM", "DMA1", "DMA2", "DMA_Request"})

#: 这些参数不该出现在"可调"清单里：``Instance`` 就是 stream 本身（用 `streams`
#: 表达）、``DMA_Handle`` 是 CubeMX 生成的句柄名、``IpInstance`` 是归属标记。
NOT_SETTABLE = frozenset({"Instance", "IpInstance", "DMA_Handle"})


@dataclass(frozen=True)
class DmaRequest:
    """一个 DMA 请求（``USART1_RX``）。"""

    name: str
    #: 归属的外设实例（``IpInstance``），可能有多个
    owners: tuple[str, ...] = ()
    base: str | None = None
    #: 库里**写死**的取值：``(参数名, 值)``。硬件定死的（``Channel``/
    #: ``Direction``/``PeriphInc``），GUI 里也不给改。
    fixed: tuple[tuple[str, str], ...] = ()
    #: 能承载它的 stream，按文件顺序
    streams: tuple[str, ...] = ()
    #: 可设的参数名（已含 `BaseMode` 继承），按展开顺序
    params: tuple[str, ...] = ()

    def belongs_to(self, instance: str) -> bool:
        return instance in self.owners

    @property
    def fixed_map(self) -> dict[str, str]:
        return dict(self.fixed)

    @property
    def settable(self) -> tuple[str, ...]:
        """用户真正能改的参数名（去掉 stream/句柄/归属这些派生项）。"""
        fixed_names = set(self.fixed_map)
        return tuple(
            p for p in self.params if p not in NOT_SETTABLE and p not in fixed_names
        )

    def __repr__(self) -> str:
        return f"DmaRequest({self.name} -> {len(self.streams)} streams)"


class DmaDb:
    """一份 ``DMA-*_Modes.xml`` 的解析结果。"""

    def __init__(
        self,
        modes: ModeDb,
        requests: dict[str, DmaRequest],
        path: Path | None = None,
    ) -> None:
        #: 参数定义/取值范围的来源，原样给调用方用（``param``/``default_value``）
        self.modes = modes
        self.requests = requests
        self.path = path

    @classmethod
    def load(cls, path: str | Path) -> DmaDb:
        p = Path(path)
        if not p.exists():
            raise DbError(f"DMA 请求表不存在: {p}")
        return _load_cached(str(p))

    # ---------------------------------------------------------------- 查询

    def request(self, name: str) -> DmaRequest | None:
        return self.requests.get(name)

    def all(self) -> tuple[DmaRequest, ...]:
        """全部请求，按文件顺序。"""
        return tuple(self.requests.values())

    def requests_for(self, instance: str) -> tuple[DmaRequest, ...]:
        """某个外设实例可用的 DMA 请求，按文件顺序。"""
        return tuple(r for r in self.requests.values() if r.belongs_to(instance))

    def param(self, name: str) -> ParamDef | None:
        return self.modes.param(name)

    def default_value(self, param: str) -> str | None:
        return self.modes.default_value(param)

    def __repr__(self) -> str:
        return f"DmaDb({len(self.requests)} requests)"


def _outer_mlos(el) -> list:
    """最外层的 ``<ModeLogicOperator>`` —— 自身不再套在另一个里面的那些。

    不能直接 ``children(root, …)``：真实文件根元素就是 ``<IP>``（没有 ``<Mcu>``
    外壳），但按 ``<Mcu><IP>`` 包一层也是合法的 XML。包一层就一个 stream 都读
    不出来，而且是**静默**的 —— 正是这份文件最该避免的失败方式。
    """
    out = []
    for c in el:
        if local(c.tag) == "ModeLogicOperator":
            out.append(c)
        else:
            out.extend(_outer_mlos(c))
    return out


def _stream_tree(root) -> dict[str, tuple[str, ...]]:
    """``<ModeLogicOperator>`` 树 -> ``{请求名: (stream, …)}``。

    只在**父子结构**上有意义，所以必须递归走（`descendants` 把树拍平了，
    拿到的 stream 和请求分不清谁套着谁）。
    """
    out: dict[str, list[str]] = {}

    def walk(mlo) -> None:
        for m in children(mlo, "Mode"):
            name = m.get("Name")
            if not name:
                continue
            nested = [
                sub for sub in children(m, "ModeLogicOperator")
            ]
            if STREAM_RE.match(name):
                # stream 的下一层 XOR 直接挂着能用它的请求
                for sub in nested:
                    for req in children(sub, "Mode"):
                        rname = req.get("Name")
                        if rname and name not in out.setdefault(rname, []):
                            out[rname].append(name)
                continue
            for sub in nested:
                walk(sub)

    for mlo in _outer_mlos(root):
        walk(mlo)
    return {k: tuple(v) for k, v in out.items()}


def _read_requests(root, modes: ModeDb) -> dict[str, DmaRequest]:
    streams_of = _stream_tree(root)
    out: dict[str, DmaRequest] = {}

    for el in descendants(root, "RefMode"):
        name = el.get("Name")
        if not name or name in NON_REQUEST_MODES:
            continue
        if (el.get("Abstract") or "").lower() == "true":
            continue

        owners: list[str] = []
        fixed: list[tuple[str, str]] = []
        for pa in children(el, "Parameter"):
            pname = pa.get("Name")
            if not pname:
                continue
            vals = [
                (v.get("Value") or (v.text or "").strip())
                for v in children(pa, "PossibleValue")
            ]
            vals = [v for v in vals if v]
            if pname == "IpInstance":
                owners.extend(v for v in vals if v not in owners)
                continue
            if len(vals) == 1:
                fixed.append((pname, vals[0]))

        prev = out.get(name)
        out[name] = DmaRequest(
            name=name,
            owners=tuple(owners) or (prev.owners if prev else ()),
            base=el.get("BaseMode") or (prev.base if prev else None),
            fixed=tuple(fixed) or (prev.fixed if prev else ()),
            streams=streams_of.get(name, prev.streams if prev else ()),
            # 排序：`mode_params` 给的是 frozenset，直接 tuple() 出来的顺序随
            # 哈希种子变 —— 同一个工程两次运行会打印出不同顺序，测试也会随机红。
            params=tuple(sorted(modes.mode_params(name))),
        )

    return out


@lru_cache(maxsize=8)
def _load_cached(path: str) -> DmaDb:
    root = parse(path)
    modes = ModeDb.load(path)  # 同一次 parse，`xml.parse` 有缓存，不额外读盘
    requests = _read_requests(root, modes)

    if not requests:
        raise DbError(f"{path}: 没解析出任何 DMA 请求，格式可能变了")
    return DmaDb(modes, requests, Path(path))


__all__ = ["DbError", "DmaDb", "DmaRequest", "STREAM_RE"]
