"""CubeMX 数据库 XML 的读取辅助。

库里的 XML 都带默认命名空间 ``http://mcd.rou.st.com/modules.php?name=mcu``，
``ElementTree`` 会把标签记成 ``{ns}Tag``，直接 ``findall("Pin")`` 一个都找不到。
这里统一按**本地名**比较，调用方不必关心命名空间。

解析结果只读，按 ``(路径, mtime, size)`` 缓存 —— 遍历 ``db/mcu`` 时同一份文件
会被反复打开，缓存能把「列一下这颗 MCU 的引脚」从秒级降到毫秒级。
"""

from __future__ import annotations

import functools
import xml.etree.ElementTree as ET
from pathlib import Path

Element = ET.Element


class DbError(LookupError):
    """数据库文件缺失或无法解析。"""


def local(tag: str) -> str:
    """``"{ns}Pin"`` -> ``"Pin"``。"""
    return tag.rsplit("}", 1)[-1]


def parse(path: str | Path) -> Element:
    """解析一份 XML（带缓存）。"""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        raise DbError(f"数据库文件不存在: {p}") from exc
    return _parse_cached(str(p), st.st_mtime_ns, st.st_size)


@functools.lru_cache(maxsize=32)
def _parse_cached(path: str, _mtime: int, _size: int) -> Element:
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise DbError(f"XML 解析失败: {path}: {exc}") from exc


def children(el: Element, name: str) -> list[Element]:
    """直接子元素里本地名为 ``name`` 的那些。"""
    return [c for c in el if local(c.tag) == name]


def descendants(el: Element, name: str) -> list[Element]:
    """任意深度的后代里本地名为 ``name`` 的那些。"""
    return [c for c in el.iter() if local(c.tag) == name]


def child(el: Element, name: str) -> Element | None:
    """第一个本地名为 ``name`` 的直接子元素。"""
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def text_of(el: Element, name: str) -> str | None:
    """子元素的文本，去空白；没有则 None。"""
    c = child(el, name)
    if c is None or c.text is None:
        return None
    return c.text.strip() or None
