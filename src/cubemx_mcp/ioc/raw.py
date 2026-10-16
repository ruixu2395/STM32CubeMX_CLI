"""`.ioc` 的行级读写。

`.ioc` 是 CubeMX 的私有格式：扁平 ``key=value``、无 section、键按 ordinal 排序、
CRLF 行尾、值里的 ``:`` 以 ``\\:`` 转义。层级完全靠点号键名表达
（``Mcu.Pin0``、``PA9.Signal``、``USART1.VirtualMode``）。

设计上的核心约束是**无损**：

* ``entries`` 存的是**原始转义态**字符串，读入时不反转义。只有上层 model 需要
  明文时才解码。这样即便我们对转义规则的了解有偏差，未触碰的键也能字节级保留。
* 未知键一律保留。CubeMX 的 schema 不公开且随版本变化，丢弃"不认识"的键等于
  静默损坏用户工程。
* 写出顺序固定为键的 ordinal 排序，与 CubeMX 自身的输出一致。

因此 ``load(path).dump(path)`` 对一个未经修改的文件必须是字节级幂等的。
"""

from __future__ import annotations

from pathlib import Path

from .escape import escape, unescape

HEADER = "#MicroXplorer Configuration settings - do not modify"

DEFAULT_NEWLINE = "\r\n"
DEFAULT_ENCODING = "utf-8"
BOM = "﻿"


class IocFormatError(ValueError):
    """`.ioc` 内容不符合预期格式。"""


class IocDocument:
    """一份 `.ioc` 的原始键值集合。

    ``entries`` 的键与值都是文件中的**转义态**字符串。需要明文时用 `get` /
    `get_all`，写入明文时用 `set` / `set_many`。
    """

    def __init__(
        self,
        entries: dict[str, str] | None = None,
        *,
        header: str = HEADER,
        newline: str = DEFAULT_NEWLINE,
        encoding: str = DEFAULT_ENCODING,
        bom: bool = False,
    ) -> None:
        self.entries: dict[str, str] = dict(entries or {})
        self.header = header
        self.newline = newline
        self.encoding = encoding
        self.bom = bom

    # ---------------------------------------------------------------- 读入

    @classmethod
    def load(cls, path: str | Path) -> IocDocument:
        """读取 `.ioc`。格式非法时抛 `IocFormatError`（带文件名与行号）。"""
        p = Path(path)
        # newline="" 让 Python 不做任何行尾转换，保留原始字节形态
        with p.open("r", encoding="utf-8", newline="") as fh:
            text = fh.read()

        if not text:
            raise IocFormatError(f"{p}: 文件为空")

        bom = text.startswith(BOM)
        if bom:
            text = text[len(BOM):]

        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(newline)
        if lines and lines[-1] == "":
            lines.pop()  # 末尾换行切出来的空串
        if not lines:
            raise IocFormatError(f"{p}: 文件为空")

        header = lines[0]
        entries: dict[str, str] = {}
        for lineno, line in enumerate(lines[1:], start=2):
            if not line.strip():
                continue  # 空行不含信息，跳过
            key, sep, value = line.partition("=")
            if not sep:
                raise IocFormatError(
                    f"{p}:{lineno}: 既不是空行也不是 key=value 行: {line!r}"
                )
            key = key.strip()
            if not key:
                raise IocFormatError(f"{p}:{lineno}: 键为空: {line!r}")
            if key in entries:
                raise IocFormatError(f"{p}:{lineno}: 键重复: {key!r}")
            entries[key] = value

        return cls(entries, header=header, newline=newline, bom=bom)

    # ---------------------------------------------------------------- 写出

    def render(self) -> str:
        """渲染成文件内容（含 header 与末尾换行）。"""
        lines = [self.header]
        lines.extend(f"{key}={self.entries[key]}" for key in sorted(self.entries))
        prefix = BOM if self.bom else ""
        return prefix + self.newline.join(lines) + self.newline

    def dump(self, path: str | Path) -> None:
        """写出到文件。无 BOM、保留检测到的行尾、末尾有换行。"""
        Path(path).write_text(
            self.render(), encoding=self.encoding, newline=""
        )

    # ------------------------------------------------------------ 原始读写

    def get_raw(self, key: str, default: str | None = None) -> str | None:
        """取转义态原值。"""
        return self.entries.get(key, default)

    def get(self, key: str, default: str | None = None) -> str | None:
        """取明文值（自动反转义）。"""
        raw = self.entries.get(key)
        return default if raw is None else unescape(raw)

    def set(self, key: str, value: str | None) -> None:
        """按明文写入；``value`` 为 None 时删除该键。"""
        if value is None:
            self.entries.pop(key, None)
        else:
            self.entries[key] = escape(value)

    def set_raw(self, key: str, value: str) -> None:
        """按转义态原值写入（不做任何转义）。"""
        self.entries[key] = value

    def discard(self, key: str) -> bool:
        """删除键，返回是否确实删掉了。"""
        return self.entries.pop(key, None) is not None

    # ---------------------------------------------------------------- 杂项

    def keys_with_prefix(self, prefix: str) -> list[str]:
        """所有以 ``prefix`` 开头的键（ordinal 序）。"""
        return sorted(k for k in self.entries if k.startswith(prefix))

    def __contains__(self, key: object) -> bool:
        return key in self.entries

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:
        return f"IocDocument({len(self.entries)} entries)"
