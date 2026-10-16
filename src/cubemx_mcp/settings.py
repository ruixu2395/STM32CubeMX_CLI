"""用户级配置：记住 CubeMX 的数据库装在哪。

打包成 exe 发给别人之后，"CubeMX 装在哪"不能再靠探测常见路径猜 —— 装在别的盘、便携版、
或者对方机器上根本没装在默认位置，都会猜空。所以第一次跑的时候问一次，存下来，以后直读。

存 ``%APPDATA%\\cubemx-cli\\config.json``（可用 ``CUBEMX_CLI_CONFIG`` 覆盖，测试靠它隔离）。

用 JSON 而不是 TOML：``tomllib`` 只能读不能写，写 TOML 要么加第三方依赖要么手搓 ——
而这个包刻意保持着**零第三方依赖**（``pyproject.toml`` 里 ``dependencies = []``）。

**本模块不碰 stdout，也不决定"要不要问用户"** —— 那是 `cli.py` 的事。这里只负责
"存了什么"和"存得对不对"。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: 配置结构版本。将来改结构时用它做迁移；现在只有 1。
VERSION = 1

#: 覆盖配置文件位置的变量。测试用它把配置层指向 tmp，避免读到开发机上的真实配置。
CONFIG_ENV = "CUBEMX_CLI_CONFIG"


def config_path() -> Path:
    """配置文件位置。``CUBEMX_CLI_CONFIG`` 优先。"""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "cubemx-cli" / "config.json"
    # 非 Windows（或 %APPDATA% 被清掉）时的退路
    return Path.home() / ".config" / "cubemx-cli" / "config.json"


# ------------------------------------------------------------------ 认目录


def is_db_root(path: Path) -> bool:
    """这个目录是不是 CubeMX 的 ``db/mcu``？

    判据是**顶层至少有一个 ``STM32*.xml`` 且不以 ``_Modes.xml`` 结尾**。

    这条判据在真实数据库（2126 个文件）上验证过，能准确区分最常见的三种指法 ——
    而"目录里有任意 .xml"这种朴素判据会被中间的 ``db`` 蒙混过去：

    | 目录 | 顶部 STM32*.xml | 结果 |
    |---|---|---|
    | ``db/mcu`` | 2123 个 | 接受 |
    | ``db`` | 0 个（只有 ``package.xml``） | 拒绝 |
    | ``db/mcu/IP`` | 11 个，但全是 ``*_Modes.xml`` | 拒绝 |
    """
    if not path.is_dir():
        return False
    try:
        return any(
            not p.name.endswith("_Modes.xml") for p in path.glob("STM32*.xml")
        )
    except OSError:
        return False


def find_db_root(candidate: Path) -> Path | None:
    """从用户给的路径里认出 ``db/mcu``，认不出返回 None。

    允许他直接给 **CubeMX 安装目录**（``D:\\stm32CubeMX``）或 ``db``，我们往下一层找 ——
    要求用户知道 ``db\\mcu`` 这一层并不友好，而多找一层是免费的。

    ``candidate`` 必须本身是个已存在的目录；不做任何猜测式的模糊匹配。
    """
    if is_db_root(candidate):
        return candidate
    for sub in ("db/mcu", "db", "mcu"):
        found = candidate / sub
        if is_db_root(found):
            return found
    return None


# ------------------------------------------------------------------ 读写


@dataclass
class Settings:
    """存下来的那几项，外加读的时候遇到的问题。"""

    db_root: Path | None = None
    cube_mx_exe: Path | None = None
    #: 文件坏了 / 版本对不上 —— 不是致命错误，但要说出来
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """写进 JSON 的形态。**逐字段写死**，不用 ``__dict__`` —— 后者会把将来新增的
        内部字段也漏进文件，变成事实上的公开格式。"""
        out: dict = {"version": VERSION}
        if self.db_root:
            out["db_root"] = str(self.db_root)
        if self.cube_mx_exe:
            out["cube_mx_exe"] = str(self.cube_mx_exe)
        return out


def load(path: Path | None = None) -> Settings:
    """读配置。**文件不存在是正常情况**（就是"还没配过"），返回空配置、无 problem。

    文件损坏时同样不抛异常，而是返回空配置 + 一条 problem —— 一个手改坏的 JSON
    不该让整个工具用不了，退化成"没配过"、让用户重配一次是合理的。
    """
    p = path or config_path()
    if not p.exists():
        return Settings()

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Settings(problems=[f"配置文件读不了（{p}）：{exc}。当作没配过。"])

    if not isinstance(raw, dict):
        return Settings(problems=[f"配置文件不是 JSON 对象（{p}）。当作没配过。"])

    problems: list[str] = []
    version = raw.get("version")
    if version is not None and version != VERSION:
        problems.append(
            f"配置文件写的是版本 {version}，本工具认的是 {VERSION}；按已知字段尽量读。"
        )

    def as_path(key: str) -> Path | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            problems.append(f"配置文件里的 {key} 不是一个路径字符串，已忽略。")
            return None
        return Path(value)

    return Settings(
        db_root=as_path("db_root"),
        cube_mx_exe=as_path("cube_mx_exe"),
        problems=problems,
    )


def save(settings: Settings, path: Path | None = None) -> Path:
    """原子写出。失败会抛 ``OSError`` —— **由调用方决定要不要致命**。

    `cli.py` 的选择是不致命：值用在本次运行上，提示一句，照常干活。存不了配置
    （只读的 ``%APPDATA%``、企业策略）不该让一条命令整个失败。

    落盘方式是"同目录临时文件 + ``os.replace``"，与 `ioc/project.py` 写 ``.ioc``
    用的是同一套 —— 半截的配置文件不会出现。
    """
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n"

    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


__all__ = [
    "CONFIG_ENV",
    "VERSION",
    "Settings",
    "config_path",
    "find_db_root",
    "is_db_root",
    "load",
    "save",
]
