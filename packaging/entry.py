"""打包成 exe 的入口。

**不能拿 `src/cubemx_mcp/__main__.py` 当入口** —— 它用的是相对导入（``from .cli``），
`python __main__.py` 和 PyInstaller 直接拿它当脚本都会炸。这里是薄薄一层绝对导入。

`SystemExit(main())` 保住退出码：0/1/2 是 Skill 和脚本依赖的契约，被 exe 包装吃掉就没了。
"""

from __future__ import annotations

import sys


def _fix_windows_console() -> None:
    """让中文在 cmd.exe 里能正常打出来。

    exe 在 cmd 下跑时，``sys.stdout.encoding`` 跟着控制台代码页走。中文简体 Windows
    默认是 cp936，这个环境里实测是 cp437 —— 编码不对时 CLI 打的中文（"错误："、
    "校验未通过"、"已写入"）要么 UnicodeEncodeError 直接崩，要么糊成乱码。

    两步都得做，管的是两件事：

    * ``SetConsoleOutputCP(65001)`` —— 告诉**这个控制台**用 UTF-8。管的是别人怎么看它。
    * ``reconfigure(encoding="utf-8")`` —— 管我们自己往流里写什么字节。光设代码页不够：
      Python 的 stdout 编码在启动时就定好了。

    重定向到文件时也一样要设 —— 那时 ``--json`` 的载荷会按 UTF-8 落盘，而不是按
    当时那台机器的本地编码，别人拿到才解析得动。

    ``errors="replace"`` 是兜底：万一流重配不了或者平台不配合，宁可打一个 ``?`` 出来，
    也不要让一条本来成功的命令死在最后一步打印上。
    """
    if sys.platform != "win32":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)  # 输入侧也要 —— 提示里可能让用户敲中文路径
    except Exception:  # noqa: BLE001 - 没有控制台（管道、服务）时没得设，不是错误
        pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main() -> int:
    _fix_windows_console()
    from cubemx_mcp.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
