"""`cubemx-cli config` 与首次运行提示。

这个文件里最重要的不是"配置存对没有"，而是两条**不能被违反的边界**：

1. **非交互不能读 stdin。** 打包成 exe 之后 CLI 会被脚本和 Agent 调用，那些场景下
   stdin 不是终端。要是在那儿等输入，命令会挂死 —— 而且是那种"没有任何输出、看起来
   在算"的挂死。所以下面用"一读就炸"的假 stdin 来断言，而不是只断言输出。
2. **提示只走 stderr。** stdout 是 ``--json`` 的载荷通道，混一行提示进去，调用方的
   `json.loads` 就废了。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from cubemx_mcp import config, settings
from cubemx_mcp.cli import EXIT_OK, EXIT_USAGE, main


# ============================================================== 假的终端/非终端


class FakeTty(io.StringIO):
    """像终端一样：``isatty()`` 为真、``readline()`` 按预置的行吐。

    ``io.StringIO`` 的 ``isatty()`` 本来就返回 False，这里翻过来。
    """

    def isatty(self) -> bool:
        return True


class ExplodingStdin:
    """一读就炸的 stdin —— 用来证明"非交互时压根没读"。"""

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:  # pragma: no cover - 走到这里就是 bug
        raise AssertionError("非交互场景不该读 stdin")


@pytest.fixture
def tty(monkeypatch):
    """把 CLI 变成"面对着终端"的状态。

    注意 `_is_interactive()` 里那道 Windows 控制台闸门（``GetConsoleMode``）在这儿是
    被绕过的 —— 这个测试环境没有真正的控制台，装不出来。所以这里同时替换掉
    ``sys.stdin`` 和那个判断函数，测的是**提示流程本身**：读什么、写哪、存不存。
    """

    def make(lines: str, *, interactive: bool = True):
        monkeypatch.setattr("sys.stdin", FakeTty(lines))
        monkeypatch.setattr("cubemx_mcp.cli._is_interactive", lambda: interactive)

    return make


@pytest.fixture
def install_dir(tmp_path: Path) -> Path:
    """一个假的 CubeMX 安装目录：有 db\\mcu、有 exe，也有来骗人的 db\\package.xml。"""
    base = tmp_path / "cube"
    (base / "db" / "mcu").mkdir(parents=True)
    (base / "db" / "mcu" / "STM32F407Z(E-G)Tx.xml").write_text("<Mcu/>", encoding="utf-8")
    (base / "db" / "package.xml").write_text("<P/>", encoding="utf-8")
    (base / "STM32CubeMX.exe").write_bytes(b"MZ")
    return base


@pytest.fixture
def detected(monkeypatch, install_dir: Path) -> Path:
    """让"自动探测"稳定地指向假安装目录 —— 开发机上的真 CubeMX 不该影响测试。"""
    monkeypatch.setattr(
        config, "cube_mx_exe", lambda: install_dir / "STM32CubeMX.exe"
    )
    return install_dir / "db" / "mcu"


def _no_auto_detection(monkeypatch) -> None:
    monkeypatch.setattr(config, "cube_mx_exe", lambda: None)


# ============================================================== config 子命令


def test_config_shows_where_the_database_came_from(monkeypatch, detected, capsys):
    out = capsys.readouterr()
    assert main(["config"]) == EXIT_OK
    text = capsys.readouterr().out

    assert str(detected) in text
    assert "自动探测" in text  # 来源要写出来，不然用户没法判断该不该配
    assert "还没有" in text  # 配置文件还不存在


def test_config_json_is_the_only_thing_on_stdout(monkeypatch, detected, capsys):
    """``--json`` 的 stdout 必须是纯 JSON —— 调用方直接 json.loads。"""
    assert main(["config", "--json"]) == EXIT_OK
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # 掺了别的东西这里就会炸

    assert payload["db_root"] == str(detected)
    assert payload["source"] == "auto"
    assert payload["problems"] == []


def test_set_db_saves_and_is_used_next_time(detected, tmp_ioc, capsys):
    assert main(["config", "--set-db", str(detected)]) == EXIT_OK
    assert "已保存" in capsys.readouterr().out

    saved = settings.load()
    assert saved.db_root == detected
    # 第二次跑，来源变成配置文件 —— 说明真的读上了，不只是写了个文件
    assert main(["config", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["source"] == "config"


def test_set_db_accepts_the_install_dir_and_normalises_it(install_dir: Path, capsys):
    """让用户指安装目录就行 —— 要求他知道 `db\\mcu` 这一层并不友好。"""
    assert main(["config", "--set-db", str(install_dir)]) == EXIT_OK
    assert settings.load().db_root == install_dir / "db" / "mcu"


def test_set_db_rejects_a_dir_with_no_database_under_it(tmp_path: Path, capsys):
    """**拒绝时不能留下半截配置。**"""
    junk = tmp_path / "junk"
    junk.mkdir()

    assert main(["config", "--set-db", str(junk)]) == EXIT_USAGE
    assert "没找到 db\\mcu" in capsys.readouterr().err
    assert not settings.config_path().exists()


def test_set_cube_mx_remembers_both(install_dir: Path, capsys):
    assert main(["config", "--set-cube-mx", str(install_dir)]) == EXIT_OK

    saved = settings.load()
    assert saved.db_root == install_dir / "db" / "mcu"
    assert saved.cube_mx_exe == install_dir / "STM32CubeMX.exe"


def test_set_cube_mx_rejects_a_missing_dir(tmp_path: Path, capsys):
    assert main(["config", "--set-cube-mx", str(tmp_path / "nope")]) == EXIT_USAGE
    assert "不是一个存在的目录" in capsys.readouterr().err


def test_set_db_says_when_the_config_is_not_what_takes_effect(
    monkeypatch, detected, tmp_path: Path, capsys
):
    """存进去 ≠ 生效。`ST_CUBEMX_DB` 优先级更高，不说清楚用户会以为工具坏了。"""
    env_db = tmp_path / "env" / "mcu"
    env_db.mkdir(parents=True)
    (env_db / "STM32F407Z(E-G)Tx.xml").write_text("<Mcu/>", encoding="utf-8")
    monkeypatch.setenv("ST_CUBEMX_DB", str(env_db))

    assert main(["config", "--set-db", str(detected)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "环境变量" in out and "优先级更高" in out


def test_config_does_not_prompt_even_on_a_terminal(tty, detected, capsys):
    """提示拦在 `config` 前面的话，"非交互地配一次"就永远做不到 —— 死锁。"""
    tty("")  # 用户什么都没敲；真弹提示的话这里会被读到
    assert main(["config"]) == EXIT_OK
    assert "先配一次" not in capsys.readouterr().err


# ============================================================== 首次运行提示


def test_non_interactive_never_reads_stdin(monkeypatch, detected, capsys):
    """**本次改动最要紧的一条。** Agent/脚本调用时读 stdin = 挂死。"""
    monkeypatch.setattr("sys.stdin", ExplodingStdin())
    monkeypatch.setattr("cubemx_mcp.cli._is_interactive", lambda: False)
    monkeypatch.setenv("ST_CUBEMX_ALLOWED_ROOTS", "")

    assert main(["info", "tests/data/Usb_uart.ioc"]) == EXIT_OK

    err = capsys.readouterr().err
    # 没弹提示，但说清了"现在用的是什么、怎么固定下来"
    assert "自动探测到的" in err and "config --set-db" in err
    assert not settings.config_path().exists()


def test_non_interactive_still_uses_the_auto_detected_database(
    monkeypatch, detected, tmp_ioc, capsys
):
    """非交互不配 → 但**不能因此失去数据库**：自动探测到的照样采纳。

    假数据库解析不出引脚，所以这里不跑依赖设备的命令（`modes` 那类），只看 CLI 怎么
    描述自己的处境 —— "用的是自动探测到的 X" 和 "没有找到数据库" 这两句是互斥的，
    谁出现就说明了走了哪条路。`test_cli.py` 里那一堆真跑设备的用例吃的是同一条路径。
    """
    monkeypatch.setattr("sys.stdin", ExplodingStdin())
    monkeypatch.setattr("cubemx_mcp.cli._is_interactive", lambda: False)

    assert main(["info", str(tmp_ioc)]) == EXIT_OK
    err = capsys.readouterr().err
    assert "自动探测到的" in err
    assert "没有找到 CubeMX 数据库" not in err


def test_non_interactive_with_no_database_points_at_config(monkeypatch, tmp_ioc, capsys):
    monkeypatch.setattr("sys.stdin", ExplodingStdin())
    monkeypatch.setattr("cubemx_mcp.cli._is_interactive", lambda: False)
    _no_auto_detection(monkeypatch)

    assert main(["info", str(tmp_ioc)]) == EXIT_OK
    err = capsys.readouterr().err
    assert "config --set-db" in err
    assert "没有找到 CubeMX 数据库" in err


def test_enter_with_nothing_detected_skips(monkeypatch, tmp_ioc, capsys):
    """没有默认值时回车 = 跳过。**不能猜**，也不能存一个空路径进去。"""
    monkeypatch.setattr("sys.stdin", FakeTty("\n"))
    monkeypatch.setattr("cubemx_mcp.cli._is_interactive", lambda: True)
    _no_auto_detection(monkeypatch)

    assert main(["info", str(tmp_ioc)]) == EXIT_OK
    assert "这次没配置" in capsys.readouterr().err
    assert not settings.config_path().exists()


def test_first_run_enter_saves_and_second_run_is_silent(
    tty, detected, tmp_ioc, capsys
):
    tty("\n")
    assert main(["info", str(tmp_ioc)]) == EXIT_OK

    err = capsys.readouterr().err
    assert "先配一次" in err and "已记住" in err
    assert settings.load().db_root == detected  # 存下来的就是自动探测到的那个

    # 第二次：不再问，也不再念叨"还没配置"
    tty("")
    assert main(["info", str(tmp_ioc)]) == EXIT_OK
    again = capsys.readouterr().err
    assert "先配一次" not in again and "已记住" not in again


def test_a_typo_gets_another_chance(tty, install_dir: Path, tmp_ioc, capsys):
    """打错的路径要说清为什么不行并**再问一次**。直接静默降级的话，用户会以为配上了。"""
    tty("D:\\nope\n" + str(install_dir) + "\n")

    assert main(["info", str(tmp_ioc)]) == EXIT_OK
    err = capsys.readouterr().err
    assert "没找到 db\\mcu" in err
    assert settings.load().db_root == install_dir / "db" / "mcu"


def test_eof_skips_without_claiming_the_database_is_off(
    monkeypatch, detected, tmp_ioc, capsys
):
    """Ctrl-D 跳过：**不能断言"数据库没启用"** —— 它可能照样是自动探测到的。

    早先的版本就是在这儿说了假话（`< NUL` 在 Windows 上被当成终端）。"""
    monkeypatch.setattr("sys.stdin", FakeTty(""))
    monkeypatch.setattr("cubemx_mcp.cli._is_interactive", lambda: True)

    assert main(["info", str(tmp_ioc)]) == EXIT_OK
    err = capsys.readouterr().err
    assert "这次没配置" in err
    assert "不启用数据库校验" not in err
    assert not settings.config_path().exists()


def test_three_bad_answers_give_up_instead_of_looping(monkeypatch, tmp_ioc, capsys):
    """**重问必须有上界。** 无上界的重问就是另一种挂死：输入流一直有内容
    （脚本喂了 5 行），它就一直问下去。"""
    monkeypatch.setattr("sys.stdin", FakeTty("a\nb\nc\nd\ne\n"))
    monkeypatch.setattr("cubemx_mcp.cli._is_interactive", lambda: True)
    _no_auto_detection(monkeypatch)

    assert main(["info", str(tmp_ioc)]) == EXIT_OK
    assert "连续 3 次都没认出来" in capsys.readouterr().err
    assert not settings.config_path().exists()


def test_the_prompt_goes_to_stderr_and_leaves_json_alone(tty, detected, tmp_ioc, capsys):
    """**提示绝不能污染 stdout。** 混进去一行，`json.loads` 就废了。"""
    tty("\n")

    assert main(["info", str(tmp_ioc), "--json"]) == EXIT_OK
    captured = capsys.readouterr()

    payload = json.loads(captured.out)  # 掺了提示这里就会炸
    assert payload["mcu_name"] == "STM32F407Z(E-G)Tx"
    assert "先配一次" in captured.err


def test_no_input_flag_suppresses_the_prompt(tty, detected, tmp_ioc, capsys):
    tty("\n")  # 放一行进去；真读了就会存下配置

    assert main(["--no-input", "info", str(tmp_ioc)]) == EXIT_OK
    assert not settings.config_path().exists()
    assert capsys.readouterr().err.count("先配一次") == 0


def test_no_input_also_works_after_the_subcommand(tty, detected, tmp_ioc, capsys):
    """argparse 的全局选项只能写在子命令前面，太容易踩；这里捞回来。"""
    tty("\n")

    assert main(["info", str(tmp_ioc), "--no-input"]) == EXIT_OK
    assert not settings.config_path().exists()


# ============================================================== 诊断不再把人带偏


def test_a_broken_env_var_names_itself(monkeypatch, tmp_ioc, capsys):
    """老实现里，设错的 `ST_CUBEMX_EXE` 会让人去设 `ST_CUBEMX_DB` —— 怎么设都好不了。"""
    monkeypatch.setattr("cubemx_mcp.cli._is_interactive", lambda: False)
    monkeypatch.setenv("ST_CUBEMX_EXE", "E:/nope/STM32CubeMX.exe")

    assert main(["info", str(tmp_ioc)]) == EXIT_OK
    assert "ST_CUBEMX_EXE" in capsys.readouterr().err


def test_a_broken_env_var_does_not_disable_the_database(
    device, monkeypatch, tmp_ioc, capsys
):
    """拼错一个变量不该等于把数据库关掉 —— 接着往下找，找到就用。

    这条**必须用真数据库**才测得出：假的解析不出引脚，`modes` 会以另一种理由失败，
    那样断言过的就是别的东西了。没有真数据库时 `device` 会跳过。
    """
    monkeypatch.setattr("cubemx_mcp.cli._is_interactive", lambda: False)
    monkeypatch.setenv("ST_CUBEMX_DB", "E:/nope/db/mcu")
    monkeypatch.delenv("ST_CUBEMX_EXE", raising=False)

    assert main(["modes", str(tmp_ioc), "USART1"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Asynchronous" in out  # 数据库照常起作用
