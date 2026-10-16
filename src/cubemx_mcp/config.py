"""运行期配置：CubeMX 安装位置、路径白名单、MCU 数据库位置。

数据库位置的解析有三级，顺序是**环境变量 → 用户配置文件 → 自动探测**。
前两级是明确的意图，第三级是猜 —— 所以猜测永远排在最后。

`resolve_db_root()` 除了给出路径，还给出**这个结论是怎么来的**（`source`）和
**路上遇到了什么麻烦**（`problems`）。用户说"我明明配了"的时候，一眼能看出是哪个源
派上了用场、哪个被忽略了。

CubeMX 的调用在 `cubemx_mcp.cubemx`（往返验证）。`cube_mx_exe()` 有两个用途：
当路标定位它旁边的 ``db/mcu``，以及给往返验证当可执行文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import settings as settings_mod

#: Windows 上 CubeMX 的常见安装位置，按顺序探测。
#: 注意第一条是开发机上的路径 —— 别人机器上多半不命中，这正是要做用户配置的原因。
_COMMON_EXE = (
    r"D:\stm32CubeMX\STM32CubeMX.exe",
    r"C:\ST\STM32CubeMX\STM32CubeMX.exe",
    r"C:\Program Files\STMicroelectronics\STM32CubeMX\STM32CubeMX.exe",
    r"C:\Program Files (x86)\STMicroelectronics\STM32CubeMX\STM32CubeMX.exe",
    "/Applications/STM32CubeMX.app/Contents/MacOS/STM32CubeMX",
    "/usr/local/STM32CubeMX/STM32CubeMX",
)


def cube_mx_exe() -> Path | None:
    """CubeMX 可执行文件路径。顺序：``ST_CUBEMX_EXE`` → 配置文件 → 常见安装位置。

    三级和 `resolve_db_root()` 同构，这不是对称美学：``cubemx-cli config
    --set-cube-mx`` 把路径写进的是**配置文件**，早先这里只认环境变量和探测清单，
    于是那条命令报"已保存"，下一次运行照样说"没找到"。往返验证要真的起 CubeMX，
    这个洞必须先补上 —— 否则报错里"跑 config --set-cube-mx 固定下来"是句假话。

    ``ST_CUBEMX_EXE`` 设了但路径不存在时返回 ``None``（**不回退**）—— 显式覆盖写错
    时应该报错，而不是悄悄换一个。`resolve_db_root()` 会把这种情况记进 `problems`。
    配置文件里那条失效了则继续往下探测（那是"以前配的，现在挪了"，不是本次意图）。
    """
    override = os.environ.get("ST_CUBEMX_EXE")
    if override:
        p = Path(override)
        return p if p.exists() else None

    saved = settings_mod.load().cube_mx_exe
    if saved is not None and saved.exists():
        return saved

    for candidate in _COMMON_EXE:
        p = Path(candidate)
        if p.exists():
            return p
    return None


@dataclass
class DbResolution:
    """数据库在哪 + **这个结论是怎么来的** + 路上遇到的问题。"""

    path: Path | None = None
    #: env（ST_CUBEMX_DB）/ config（用户配置文件）/ auto（探测）/ none
    source: str = "none"
    #: 给人看的一句话：具体是哪个变量、哪个文件、哪个 exe
    detail: str = ""
    #: 被忽略的源，逐条说明为什么 —— 这些是"我明明配了呀"的答案
    problems: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.path is not None


def resolve_db_root() -> DbResolution:
    """按 环境变量 → 配置文件 → 自动探测 的顺序找 ``db/mcu``。"""
    res = DbResolution()
    cfg = settings_mod.load()
    res.problems.extend(cfg.problems)

    # 1. ST_CUBEMX_DB —— 最明确的意图，优先级最高。
    override = os.environ.get("ST_CUBEMX_DB")
    if override:
        p = Path(override)
        if p.is_dir():
            if not settings_mod.is_db_root(p):
                # 是目录但不是 db/mcu：最常见的错是少给一层（指到 db 去了）。
                # **不拒绝** —— 用户显式给的路径就该认，只用 problem 提醒他可能是哪错了。
                res.problems.append(
                    f"ST_CUBEMX_DB={override} 存在，但里面没有 STM32*.xml ——"
                    f" 它看上是 CubeMX 的 db 目录而不是 db\\mcu。"
                )
            return replace(res, path=p, source="env", detail=f"环境变量 ST_CUBEMX_DB={override}")
        # 设了但根本不是目录：记下来**继续往下找**。早先的实现到这里直接返回 None，
        # 于是把变量拼错一次就等于把整个数据库功能关掉，而提示语还让人去"设置
        # ST_CUBEMX_DB" —— 明明已经设了，只是设错了。
        res.problems.append(f"ST_CUBEMX_DB={override} 不是一个存在的目录，已忽略。")

    # 2. 用户配置文件（首次运行时存的）。比自动探测可信，比环境变量弱 ——
    #    环境变量是"这一次就用这个"，配置文件是"我一直用这个"。
    if cfg.db_root:
        if cfg.db_root.is_dir():
            if not settings_mod.is_db_root(cfg.db_root):
                res.problems.append(
                    f"配置文件里的 db_root={cfg.db_root} 里没有 STM32*.xml，"
                    f"可能指向了 db 而不是 db\\mcu。跑 cubemx-cli config 重新配。"
                )
            return replace(
                res, path=cfg.db_root, source="config",
                detail=f"配置文件 {settings_mod.config_path()}",
            )
        res.problems.append(
            f"配置文件里的 db_root={cfg.db_root} 不存在（CubeMX 挪位置了？），已忽略。"
        )

    # 3. 自动探测：拿 CubeMX.exe 当路标，找它旁边的 db/mcu。
    exe_override = os.environ.get("ST_CUBEMX_EXE")
    exe = cube_mx_exe()
    if exe is not None:
        p = exe.parent / "db" / "mcu"
        if p.is_dir():
            return replace(res, path=p, source="auto", detail=f"自动探测到 {exe}")
        res.problems.append(f"自动探测到 {exe}，但它旁边没有 db\\mcu。")
    elif exe_override:
        res.problems.append(
            f"ST_CUBEMX_EXE={exe_override} 不存在，自动探测已跳过。"
        )

    return res


def db_root() -> Path | None:
    """CubeMX 的 MCU 数据库目录（``db/mcu``），用于引脚能力校验。

    只是 `resolve_db_root()` 的薄包装 —— 调用点因此一处都不用改。需要知道
    **路径是怎么来的**（做诊断、或者要解释"为什么校验被跳过"）时用 `resolve_db_root()`。
    """
    return resolve_db_root().path


def allowed_roots() -> list[Path]:
    """允许读写的根目录。为空表示不限制（仅建议在本机自用时）。"""
    raw = os.environ.get("ST_CUBEMX_ALLOWED_ROOTS", "")
    return [Path(p).resolve() for p in raw.split(os.pathsep) if p.strip()]
