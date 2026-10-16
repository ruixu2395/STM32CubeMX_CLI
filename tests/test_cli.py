"""`cubemx_mcp.cli` —— 命令行入口。

最重要的一组是**写闸门**：校验不过时磁盘上的文件必须一个字节都不变。
这不是"顺手测一下"，而是整个工具敢让 AI 直接调的前提。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cubemx_mcp import cubemx
from cubemx_mcp.cli import EXIT_INVALID, EXIT_OK, EXIT_USAGE, main


# ================================================================== 读命令


def test_info(tmp_ioc: Path, capsys):
    assert main(["info", str(tmp_ioc)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "STM32F407Z(E-G)Tx" in out and "STM32F407ZGT6" in out
    assert "LQFP144" in out
    assert "USART1" in out


def test_pins_lists_configured_pins(tmp_ioc: Path, capsys):
    assert main(["pins", str(tmp_ioc)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "PA9" in out and "USART1_TX" in out and "Asynchronous" in out


def test_pins_json(tmp_ioc: Path, capsys):
    assert main(["pins", str(tmp_ioc), "--json"]) == EXIT_OK
    rows = json.loads(capsys.readouterr().out)

    assert [r["name"] for r in rows] == ["PA9", "PA10"]
    assert rows[0] == {
        "name": "PA9",
        "signal": "USART1_TX",
        "mode": "Asynchronous",
        "label": None,
        "locked": True,
        "parameters": [],
        "properties": {},
    }


def test_peripherals_lists_pins_of_each_ip(tmp_ioc: Path, capsys):
    assert main(["peripherals", str(tmp_ioc), "--json"]) == EXIT_OK
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}

    assert rows["USART1"]["virtual"] == {"VirtualMode": "VM_ASYNC"}
    assert rows["USART1"]["pins"] == ["PA9", "PA10"]


def test_modes_lists_what_the_db_offers(tmp_ioc: Path, capsys):
    assert main(["modes", str(tmp_ioc), "USART2"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Asynchronous" in out and "VirtualMode=VM_ASYNC" in out and "RX, TX" in out


def test_modes_json_gives_the_value_not_the_display_string(tmp_ioc: Path, capsys):
    """**这条是给 AI 用的。**

    文本形态打印 `VirtualMode=VM_ASYNC` 是给人读的；JSON 里必须拆成能直接喂回
    `--virtual-mode` 的**值**，否则 Agent 把整串抄过去只会得到"参数不认识"。
    `virtual_key` 顺带告诉它 SPI 用的是 `--virtual-type`。
    """
    assert main(["modes", str(tmp_ioc), "USART2", "--json"]) == EXIT_OK
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}

    assert rows["Asynchronous"]["virtual"] == "VM_ASYNC"
    assert rows["Asynchronous"]["virtual_key"] == "VirtualMode"
    assert rows["Asynchronous"]["signals"] == ["RX", "TX"]

    # 拿回来的值必须真的能用
    assert main(["set-peripheral", str(tmp_ioc), "USART2",
                 "--virtual-mode", rows["Asynchronous"]["virtual"]]) == EXIT_OK


def test_modes_json_says_virtual_type_for_spi(tmp_ioc: Path, capsys):
    """同一个字段 SPI 的名字不同 —— 这正是最容易配错的地方。"""
    assert main(["modes", str(tmp_ioc), "SPI2", "--json"]) == EXIT_OK
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["Full_Duplex_Master"]["virtual_key"] == "VirtualType"
    assert rows["Full_Duplex_Master"]["virtual"] == "VM_MASTER"


def test_info_json(tmp_ioc: Path, capsys):
    assert main(["info", str(tmp_ioc), "--json"]) == EXIT_OK
    info = json.loads(capsys.readouterr().out)

    assert info["mcu_name"] == "STM32F407Z(E-G)Tx"
    assert info["cpn"] == "STM32F407ZGT6"
    assert info["package"] == "LQFP144"
    assert info["pins_configured"] == 2
    assert "USART1" in info["peripherals"]


def test_show_prints_the_file_verbatim(tmp_ioc: Path, capsys):
    assert main(["show", str(tmp_ioc)]) == EXIT_OK
    # newline="" 才拿得到 CRLF，否则 read_text 会按通用换行把 \r\n 折成 \n
    assert capsys.readouterr().out == tmp_ioc.read_text(encoding="utf-8", newline="")


def test_validate_on_a_clean_project(tmp_ioc: Path, capsys):
    assert main(["validate", str(tmp_ioc)]) == EXIT_OK
    assert "通过" in capsys.readouterr().out


def test_validate_json(tmp_ioc: Path, capsys):
    assert main(["validate", str(tmp_ioc), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["issues"] == []


def test_missing_file_is_a_usage_error(tmp_path: Path, capsys):
    assert main(["info", str(tmp_path / "nope.ioc")]) == EXIT_USAGE
    assert "不存在" in capsys.readouterr().err


def test_a_file_that_is_not_an_ioc(tmp_path: Path, capsys):
    bogus = tmp_path / "x.ioc"
    bogus.write_text("hello=world\n", encoding="utf-8", newline="")
    assert main(["info", str(bogus)]) == EXIT_USAGE
    assert "Mcu.Name" in capsys.readouterr().err


# ============================================================== 写闸门


def test_set_pin_writes_and_backs_up(tmp_ioc: Path, capsys):
    assert main(["set-pin", str(tmp_ioc), "PA2",
                 "--signal", "USART2_TX", "--mode", "Asynchronous"]) == EXIT_OK

    text = tmp_ioc.read_text(encoding="utf-8")
    # 逐字段命令只写被点到的那几个键 —— VirtualMode 要另说，它不猜
    assert "PA2.Signal=USART2_TX" in text
    assert "PA2.Mode=Asynchronous" in text
    assert "PA2.Locked=true" not in text  # --lock 没给就不写
    assert "Mcu.IP4=USART2" in text  # 归属的外设自动登记
    assert "USART2.VirtualMode" not in text
    assert list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))
    assert "已写入" in capsys.readouterr().out


def test_invalid_result_is_never_written(tmp_ioc: Path, capsys):
    """**这是最关键的一条。** 校验不过 → 退出码 2 → 原文件一个字节都不变。"""
    before = tmp_ioc.read_bytes()

    assert main(["set-pin", str(tmp_ioc), "PA9", "--mode", "Nonsense"]) == EXIT_INVALID

    assert tmp_ioc.read_bytes() == before
    assert not list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))
    err = capsys.readouterr().err
    assert "mode.unknown" in err and "没有写入文件" in err


def test_a_wrong_key_blocks_but_an_unfinished_one_does_not(tmp_ioc: Path, capsys):
    """两种 error 要区别对待：

    "PA9 扛不住 USART2_TX" 是**配错了**，必须挡住；"USART2 还缺 RX" 是**没配完**，
    逐字段命令天然要路过这个中间状态，挡住它等于让 set-pin 一步都走不了。
    """
    before = tmp_ioc.read_bytes()

    wrong = main(["set-pin", str(tmp_ioc), "PA10", "--signal", "Nonsense_SIG"])
    assert wrong == EXIT_INVALID
    assert tmp_ioc.read_bytes() == before
    assert "pin.unsupported_signal" in capsys.readouterr().err

    # 同一个文件，只是"还没配完" → 放行
    assert main(["set-pin", str(tmp_ioc), "PA2", "--signal", "USART2_TX",
                 "--mode", "Asynchronous"]) == EXIT_OK
    assert "PA2.Signal=USART2_TX" in tmp_ioc.read_text(encoding="utf-8")
    assert "尚未配完" in capsys.readouterr().err


def test_the_documented_field_by_field_flow_works(tmp_ioc: Path):
    """用户给的流程：先 set-pin TX、再 set-pin RX、最后 set-peripheral。

    每一步单独看都是"半配置"，但合起来必须是自洽的 —— 这正是分步编辑的意义。
    """
    assert main(["set-pin", str(tmp_ioc), "PA2", "--signal", "USART2_TX",
                 "--mode", "Asynchronous"]) == EXIT_OK
    assert main(["set-pin", str(tmp_ioc), "PA3", "--signal", "USART2_RX",
                 "--mode", "Asynchronous"]) == EXIT_OK
    assert main(["set-peripheral", str(tmp_ioc), "USART2",
                 "--virtual-mode", "VM_ASYNC",
                 "--param", "BaudRate=115200"]) == EXIT_OK

    # 三步都落盘了，最后必须是一个干净工程
    assert main(["validate", str(tmp_ioc)]) == EXIT_OK


def test_force_writes_anyway(tmp_ioc: Path, capsys):
    assert main(["set-pin", str(tmp_ioc), "PA9", "--mode", "Nonsense",
                 "--force"]) == EXIT_OK
    assert "PA9.Mode=Nonsense" in tmp_ioc.read_text(encoding="utf-8")


def test_dry_run_shows_the_diff_and_writes_nothing(tmp_ioc: Path, capsys):
    before = tmp_ioc.read_bytes()

    assert main(["add-gpio", str(tmp_ioc), "PC13", "--mode", "Output",
                 "--label", "KEY", "--dry-run"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "+PC13-ANTI_TAMP.Signal=GPIO_Output" in out
    assert "没有写入文件" in out
    assert tmp_ioc.read_bytes() == before


def test_dry_run_reports_an_invalid_result(tmp_ioc: Path, capsys):
    assert main(["set-pin", str(tmp_ioc), "PA9", "--mode", "Nonsense",
                 "--dry-run"]) == EXIT_INVALID
    assert "mode.unknown" in capsys.readouterr().out


def test_no_backup_flag(tmp_ioc: Path):
    main(["set-pin", str(tmp_ioc), "PA2", "--signal", "USART2_TX",
          "--no-backup"])
    assert not list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))


def test_a_command_that_changes_nothing_says_so(tmp_ioc: Path, capsys):
    """PA9 本来就 Locked=true，再 --lock 一次不是"改动" —— 不该白写一次盘。"""
    before = tmp_ioc.read_bytes()

    assert main(["set-pin", str(tmp_ioc), "PA9", "--lock"]) == EXIT_OK

    assert "没有可做的改动" in capsys.readouterr().out
    assert tmp_ioc.read_bytes() == before
    assert not list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))


def test_a_refused_edit_leaves_the_file_alone(tmp_ioc: Path, capsys):
    """PA9 属于 USART1，改投 USART2 要被挡住 —— 且不落盘。"""
    before = tmp_ioc.read_bytes()

    assert main(["set-pin", str(tmp_ioc), "PA9", "--signal", "USART2_TX"]) == EXIT_USAGE

    assert tmp_ioc.read_bytes() == before
    assert "USART1" in capsys.readouterr().err


# ============================================================== 写命令


def test_clear_pin(tmp_ioc: Path):
    assert main(["add-gpio", str(tmp_ioc), "PB0", "--mode", "Output",
                 "--label", "LED"]) == EXIT_OK
    assert main(["clear-pin", str(tmp_ioc), "PB0"]) == EXIT_OK

    text = tmp_ioc.read_text(encoding="utf-8")
    assert "PB0.Signal" not in text and "PB0.GPIO_Label" not in text
    assert "Mcu.PinsNb=2" in text


def test_clear_pin_leaves_a_gap_the_validation_reports(tmp_ioc: Path, capsys):
    """清掉 USART1 的 TX 之后 RX 还在，串口就"没配完"了 —— 允许落盘但要说。"""
    assert main(["clear-pin", str(tmp_ioc), "PA9"]) == EXIT_OK
    assert "mode.missing_signals" in capsys.readouterr().err

    # 最后那道总检查仍然会指出它
    assert main(["validate", str(tmp_ioc)]) == EXIT_INVALID


def test_set_peripheral(tmp_ioc: Path, capsys):
    assert main(["set-peripheral", str(tmp_ioc), "USART1",
                 "--param", "BaudRate=115200"]) == EXIT_OK
    assert "USART1.BaudRate=115200" in tmp_ioc.read_text(encoding="utf-8")
    assert "USART1.IPParameters=VirtualMode,BaudRate" in tmp_ioc.read_text(encoding="utf-8")


def test_set_peripheral_unset(tmp_ioc: Path):
    assert main(["set-peripheral", str(tmp_ioc), "USART1",
                 "--param", "BaudRate=115200"]) == EXIT_OK
    assert main(["set-peripheral", str(tmp_ioc), "USART1",
                 "--unset", "BaudRate"]) == EXIT_OK

    text = tmp_ioc.read_text(encoding="utf-8")
    assert "USART1.BaudRate" not in text
    assert "USART1.IPParameters=VirtualMode\n" in text


def test_unsetting_a_required_key_is_flagged(tmp_ioc: Path, capsys):
    """删掉 VirtualMode 会让 USART1 自相矛盾 —— 落盘但报告出来。"""
    assert main(["set-peripheral", str(tmp_ioc), "USART1",
                 "--unset", "VirtualMode"]) == EXIT_OK
    assert "virtual.missing" in capsys.readouterr().err


def test_set_peripheral_without_anything_to_do(tmp_ioc: Path, capsys):
    assert main(["set-peripheral", str(tmp_ioc), "USART1"]) == EXIT_USAGE
    assert "没有给出要改什么" in capsys.readouterr().err


def test_set_peripheral_given_a_value_that_is_already_set(tmp_ioc: Path, capsys):
    """给了参数、只是值恰好没变 —— 那是"没有可做的改动"，不是"没有给出要改什么"。

    老毛病：判空放在改动**之后**（`if not changes: raise`），于是上面那种情况会被
    骂成没给参数，用户明明给了。空改动就该走空改动那条路（退出码 0、不写盘）。
    """
    assert main(["set-peripheral", str(tmp_ioc), "USART1",
                 "--param", "BaudRate=115200"]) == EXIT_OK
    capsys.readouterr()
    before = tmp_ioc.read_bytes()
    backups = sorted(tmp_ioc.parent.glob("Proj.ioc.bak-*"))  # 上一条命令留下的那份

    assert main(["set-peripheral", str(tmp_ioc), "USART1",
                 "--param", "BaudRate=115200"]) == EXIT_OK

    captured = capsys.readouterr()
    assert "没有可做的改动" in captured.out
    assert "没有给出要改什么" not in captured.err
    assert tmp_ioc.read_bytes() == before
    assert sorted(tmp_ioc.parent.glob("Proj.ioc.bak-*")) == backups


def test_unsetting_a_key_that_is_not_there_is_not_a_change(tmp_ioc: Path, capsys):
    """`--unset` 一个本来就没有的键，不该报"已删除"（那是谎报改动），更不该写盘。"""
    before = tmp_ioc.read_bytes()

    assert main(["set-peripheral", str(tmp_ioc), "USART1",
                 "--unset", "NoSuchParam"]) == EXIT_OK

    captured = capsys.readouterr()
    assert "已删除" not in captured.out
    assert "没有可做的改动" in captured.out
    assert tmp_ioc.read_bytes() == before
    assert not list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))


def test_remove_peripheral(tmp_ioc: Path):
    assert main(["remove-peripheral", str(tmp_ioc), "USART1"]) == EXIT_OK
    text = tmp_ioc.read_text(encoding="utf-8")
    assert "Mcu.IP0=DMA" in text and "Mcu.IP3=USART1" not in text
    assert "PA9.Signal" not in text


def test_remove_peripheral_keep_pins(tmp_ioc: Path):
    assert main(["remove-peripheral", str(tmp_ioc), "USART1",
                 "--keep-pins"]) == EXIT_OK
    text = tmp_ioc.read_text(encoding="utf-8")
    assert "Mcu.Pin0=PA9" in text and "PA9.Signal" not in text


def test_add_gpio(tmp_ioc: Path):
    assert main(["add-gpio", str(tmp_ioc), "PC13", "--mode", "Output",
                 "--label", "KEY", "--set", "GPIO_PuPd=GPIO_UP"]) == EXIT_OK

    text = tmp_ioc.read_text(encoding="utf-8")
    assert "PC13-ANTI_TAMP.Signal=GPIO_Output" in text
    assert "PC13-ANTI_TAMP.GPIOParameters=GPIO_Label,GPIO_PuPd" in text
    assert "PC13-ANTI_TAMP.GPIO_Label=KEY" in text


def test_add_gpio_exti(tmp_ioc: Path):
    assert main(["add-gpio", str(tmp_ioc), "PB0", "--mode", "EXTI"]) == EXIT_OK
    assert "PB0.Signal=GPXTI0" in tmp_ioc.read_text(encoding="utf-8")


def test_add_gpio_needs_a_mode(tmp_ioc: Path, capsys):
    """``--mode`` 是必填。

    早先这里是 ``with pytest.raises(SystemExit): code == 2`` —— 那是 argparse 自带的
    行为被原样放行了。但**2 在我们的契约里是"工程校验没过"**（Skill 就是靠这个区分
    "改错了"和"命令写错了"），命令行缺参数混进 2 会让 Agent 去修一个根本没坏的工程。
    现在这类错误统一归 ``EXIT_USAGE``。
    """
    assert main(["add-gpio", str(tmp_ioc), "PB0"]) == EXIT_USAGE
    assert "--mode" in capsys.readouterr().err


def test_help_is_success_not_a_usage_error(capsys):
    """``--help`` 是成功路径。argparse 的 ``exit()`` 不带退出码，别被上一条的重映射误伤。"""
    assert main(["--help"]) == EXIT_OK
    assert "config" in capsys.readouterr().out


def test_2_is_reserved_for_an_invalid_project(tmp_ioc: Path):
    """**2 只留给"工程校验没过"这一种情况** —— 那是 Agent 分流的依据：看到 2 该去读
    校验报告，看到 1 该去改命令。argparse 的用法错误不能混进来。"""
    assert main(["set-pin", str(tmp_ioc)]) == EXIT_USAGE


# ========================================================== 意图级命令


def test_add_peripheral_auto_allocates(tmp_ioc: Path, capsys):
    assert main(["add-peripheral", str(tmp_ioc), "USART2",
                 "--param", "BaudRate=115200"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "Asynchronous" in out and "USART2_TX -> PA2" in out

    text = tmp_ioc.read_text(encoding="utf-8")
    assert "PA2.Signal=USART2_TX" in text and "PA3.Signal=USART2_RX" in text
    assert "USART2.VirtualMode=VM_ASYNC" in text
    assert "USART2.BaudRate=115200" in text
    assert "Mcu.IP4=USART2" in text


def test_add_peripheral_with_explicit_pins(tmp_ioc: Path, capsys):
    assert main(["add-peripheral", str(tmp_ioc), "USART2",
                 "--tx", "PD5", "--rx", "PD6"]) == EXIT_OK
    assert "PD5.Signal=USART2_TX" in tmp_ioc.read_text(encoding="utf-8")


def test_add_peripheral_spi_uses_virtual_type(tmp_ioc: Path):
    assert main(["add-peripheral", str(tmp_ioc), "SPI2"]) == EXIT_OK
    assert "SPI2.VirtualType=VM_MASTER" in tmp_ioc.read_text(encoding="utf-8")


def test_add_peripheral_writes_virtual_pins_for_timers(tmp_ioc: Path):
    assert main(["add-peripheral", str(tmp_ioc), "TIM3"]) == EXIT_OK
    assert "VP_TIM3_VS_ClockSourceINT" in tmp_ioc.read_text(encoding="utf-8")


def test_add_peripheral_rejects_a_bad_pin(tmp_ioc: Path, capsys):
    before = tmp_ioc.read_bytes()

    assert main(["add-peripheral", str(tmp_ioc), "USART2",
                 "--rx", "PA11", "--tx", "PA2"]) == EXIT_USAGE

    assert tmp_ioc.read_bytes() == before
    err = capsys.readouterr().err
    assert "PA11" in err and "PA3" in err  # 告诉用户 RX 该用哪个脚


def test_add_peripheral_rejects_an_unknown_peripheral(tmp_ioc: Path, capsys):
    assert main(["add-peripheral", str(tmp_ioc), "USART9"]) == EXIT_USAGE
    assert "USART9" in capsys.readouterr().err


def test_add_peripheral_rejects_an_unknown_mode(tmp_ioc: Path, capsys):
    assert main(["add-peripheral", str(tmp_ioc), "USART2",
                 "--mode", "Nonsense"]) == EXIT_USAGE
    assert "Asynchronous" in capsys.readouterr().err


def test_add_peripheral_dry_run(tmp_ioc: Path, capsys):
    before = tmp_ioc.read_bytes()

    assert main(["add-peripheral", str(tmp_ioc), "USART2", "--dry-run"]) == EXIT_OK

    assert "+USART2.VirtualMode=VM_ASYNC" in capsys.readouterr().out
    assert tmp_ioc.read_bytes() == before


# ========================================================== 用法与白名单


def test_no_command_prints_help(capsys):
    assert main([]) == EXIT_USAGE
    assert "add-peripheral" in capsys.readouterr().out


def test_unknown_option_is_rejected(tmp_ioc: Path, capsys):
    assert main(["pins", str(tmp_ioc), "--nonsense"]) == EXIT_USAGE
    assert "--nonsense" in capsys.readouterr().err


def test_add_peripheral_rejects_a_bare_positional(tmp_ioc: Path, capsys):
    """``add-peripheral`` 用 parse_known_args 收动态选项，但也不能什么都收。"""
    before = tmp_ioc.read_bytes()

    assert main(["add-peripheral", str(tmp_ioc), "USART2", "PA2"]) == EXIT_USAGE

    assert "--tx PA2" in capsys.readouterr().err  # 告诉用户正确的写法
    assert tmp_ioc.read_bytes() == before


def test_add_peripheral_rejects_a_dangling_signal_option(tmp_ioc: Path, capsys):
    """``--rr`` 是 ``--rx`` 少打一个字母。静默忽略只会让人以为配上了。"""
    before = tmp_ioc.read_bytes()

    assert main(["add-peripheral", str(tmp_ioc), "USART2", "--rr", "PA4"]) == EXIT_USAGE

    err = capsys.readouterr().err
    assert "RR" in err and "RX" in err
    assert tmp_ioc.read_bytes() == before


def test_add_peripheral_rejects_a_signal_option_with_no_value(tmp_ioc: Path, capsys):
    assert main(["add-peripheral", str(tmp_ioc), "USART2", "--tx"]) == EXIT_USAGE
    assert "引脚名" in capsys.readouterr().err


def test_add_peripheral_accepts_the_equals_form(tmp_ioc: Path):
    assert main(["add-peripheral", str(tmp_ioc), "USART2", "--tx=PD5",
                 "--rx=PD6"]) == EXIT_OK
    assert "PD5.Signal=USART2_TX" in tmp_ioc.read_text(encoding="utf-8")


def test_allowed_roots_gate_reads_too(tmp_ioc: Path, monkeypatch, capsys):
    """白名单管的是"能不能碰这个文件"，不是"能不能改"。"""
    monkeypatch.setenv("ST_CUBEMX_ALLOWED_ROOTS", str(tmp_ioc.parent / "elsewhere"))
    assert main(["info", str(tmp_ioc)]) == EXIT_USAGE
    assert "白名单" in capsys.readouterr().err


def test_allowed_roots_permits_a_listed_directory(tmp_ioc: Path, monkeypatch):
    monkeypatch.setenv("ST_CUBEMX_ALLOWED_ROOTS", str(tmp_ioc.parent))
    assert main(["info", str(tmp_ioc)]) == EXIT_OK


def test_bad_kv_pair_is_a_usage_error(tmp_ioc: Path, capsys):
    assert main(["set-peripheral", str(tmp_ioc), "USART1",
                 "--param", "BaudRate"]) == EXIT_USAGE
    assert "名=值" in capsys.readouterr().err


def test_the_original_file_survives_every_failed_command(tmp_ioc: Path):
    """一串会失败的命令跑下来，文件仍与最初逐字节相同。"""
    before = tmp_ioc.read_bytes()
    for argv in (
        ["set-pin", str(tmp_ioc), "PA9", "--mode", "Nonsense"],
        ["add-peripheral", str(tmp_ioc), "USART9"],
        ["add-peripheral", str(tmp_ioc), "USART2", "--rr", "PA4"],
        ["set-peripheral", str(tmp_ioc), "USART1", "--param", "NoSuchParam=1"],
        ["remove-peripheral", str(tmp_ioc), "UART7"],
        ["enable-irq", str(tmp_ioc), "NoSuch_IRQn"],
        ["add-dma", str(tmp_ioc), "USART9_RX"],
        ["remove-dma", str(tmp_ioc), "USART1"],
    ):
        assert main(argv) != EXIT_OK, argv
    assert tmp_ioc.read_bytes() == before


# ============================================================== 中断与 DMA


def test_enable_irq_on_an_existing_key_keeps_the_tail_byte_for_byte(
    tmp_ioc: Path, capsys
) -> None:
    """**本组最要紧的一条。**

    ``NVIC.<IRQn>`` 的值是变长元组，第 3 段往上是 CubeMX 的**版本印记**（4.13 是
    4 段、4.15–4.19 是 6 段、5.x 是 8 段、6.x 是 9 段），同一个 IRQ 名在不同文件里
    尾巴还会变。所以它既推演不出来、也不能照抄别的文件 —— 只能**逐字节带着走**。
    这里用改优先级来验证：尾巴只要动了一个字符，CubeMX 下次加载就会按它的规则重写，
    而那是我们完全看不见的一次静默改动。
    """
    assert main(["enable-irq", str(tmp_ioc), "USART1_IRQn",
                 "--preemption", "5"]) == EXIT_OK

    line = [ln for ln in tmp_ioc.read_text(encoding="utf-8").splitlines()
            if ln.startswith("NVIC.USART1_IRQn=")][0]
    assert line == r"NVIC.USART1_IRQn=true\:5\:0\:false\:false\:true\:true\:true\:true"
    assert "后 6 段原样保留" in capsys.readouterr().out


def test_enable_irq_on_a_missing_key_writes_only_the_minimal_triple(
    tmp_ioc: Path,
) -> None:
    """键不存在时只写最小的三段式，**绝不自己编尾巴**。

    实测：手写 `true:0:0` 进去，CubeMX 加载-保存后自己补成 9 段。让 CubeMX 补全
    是唯一站得住的写法。
    """
    assert main(["disable-irq", str(tmp_ioc), "USART1_IRQn"]) == EXIT_OK
    assert main(["enable-irq", str(tmp_ioc), "USART1_IRQn",
                 "--preemption", "7"]) == EXIT_OK

    line = [ln for ln in tmp_ioc.read_text(encoding="utf-8").splitlines()
            if ln.startswith("NVIC.USART1_IRQn=")][0]
    assert line == r"NVIC.USART1_IRQn=true\:7\:0"


def test_disable_irq_deletes_the_line_rather_than_writing_false(
    tmp_ioc: Path,
) -> None:
    r""""关中断"在这份文件里的真身是**没有这一行**。

    手写 `NVIC.X_IRQn=false\:...` 进去，CubeMX 加载时整行删掉 —— 写 `false` 是绕一圈
    回到同一处，还多一个 CubeMX 自己都不认的中间态。
    """
    assert main(["disable-irq", str(tmp_ioc), "USART1_IRQn"]) == EXIT_OK

    text = tmp_ioc.read_text(encoding="utf-8")
    assert "NVIC.USART1_IRQn" not in text
    # 整份文件里一行 `NVIC.*=false` 都不该有 —— 那种中间态 CubeMX 自己不认
    assert not [ln for ln in text.splitlines() if ln.startswith("NVIC.") and "=false" in ln]


def test_enable_irq_rejects_an_unknown_name_and_lists_candidates(
    tmp_ioc: Path, capsys
) -> None:
    """IRQ 名零校验是第 1 步留下的老毛病：拼错了会被当成"新建"写进去。"""
    before = tmp_ioc.read_bytes()

    assert main(["enable-irq", str(tmp_ioc), "USART9_IRQn"]) == EXIT_USAGE

    err = capsys.readouterr().err
    assert "USART9_IRQn" in err and "USART1_IRQn" in err
    assert tmp_ioc.read_bytes() == before


def test_irq_name_candidates_survive_an_all_lowercase_typo(
    tmp_ioc: Path, capsys
) -> None:
    """整串小写也要给候选 —— 名字大小写敏感（拒绝是对的），但"相近的有"是给人
    指路的，没有理由跟着敏感。实测没这一档时 `usart1_irqn` 一个候选都不给。
    """
    before = tmp_ioc.read_bytes()

    assert main(["enable-irq", str(tmp_ioc), "usart1_irqn"]) == EXIT_USAGE

    err = capsys.readouterr().err
    assert "USART1_IRQn" in err, "小写拼错时该照样列出候选"
    assert tmp_ioc.read_bytes() == before


def test_enable_irq_rejects_a_priority_out_of_range(tmp_ioc: Path, capsys) -> None:
    before = tmp_ioc.read_bytes()

    assert main(["enable-irq", str(tmp_ioc), "USART1_IRQn",
                 "--preemption", "99"]) == EXIT_USAGE

    assert "99" in capsys.readouterr().err
    assert tmp_ioc.read_bytes() == before


def test_enabling_an_irq_that_is_already_like_that_is_not_a_change(
    tmp_ioc: Path, capsys
) -> None:
    """幂等：USART1_IRQn 本来就是 `true:0:0:…`，再开一次不该白写盘、白留备份。"""
    before = tmp_ioc.read_bytes()

    assert main(["enable-irq", str(tmp_ioc), "USART1_IRQn",
                 "--preemption", "0"]) == EXIT_OK

    assert "没有可做的改动" in capsys.readouterr().out
    assert tmp_ioc.read_bytes() == before
    assert not list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))


def test_add_dma_picks_a_stream_and_prints_it(tmp_ioc: Path, capsys) -> None:
    assert main(["add-dma", str(tmp_ioc), "USART2_RX"]) == EXIT_OK

    out = capsys.readouterr().out
    text = tmp_ioc.read_text(encoding="utf-8")
    stream = [ln.split("=", 1)[1] for ln in text.splitlines()
              if ln.startswith("Dma.USART2_RX.2.Instance=")][0]
    # 挑中的那个 stream 必须打在改动清单里 —— 那是这份配置里最要紧的一项
    assert stream in out
    assert "Dma.Request2=USART2_RX" in text
    assert "Dma.RequestsNb=3" in text


def test_add_dma_rejects_a_stream_the_request_cannot_use(
    tmp_ioc: Path, capsys
) -> None:
    before = tmp_ioc.read_bytes()

    assert main(["add-dma", str(tmp_ioc), "USART2_RX",
                 "--stream", "DMA1_Stream0"]) == EXIT_USAGE

    err = capsys.readouterr().err
    assert "可选：" in err and "DMA1_Stream0" in err
    assert tmp_ioc.read_bytes() == before


def test_remove_dma_renumbers_the_slots_after_it(tmp_ioc: Path, capsys) -> None:
    """删中间那个（USART1_TX 在槽位 1），后面的请求整块落到 1 号槽。

    `Dma.Request<n>` 与 `Dma.<请求>.<n>.*` 里的 `n` 是**同一个全局序号**，搬错一格
    就是一次静默损坏 —— 所以这里盯着键集合断言，不是盯着 `RequestsNb`。
    """
    assert main(["add-dma", str(tmp_ioc), "USART2_RX"]) == EXIT_OK
    capsys.readouterr()

    assert main(["remove-dma", str(tmp_ioc), "USART1_TX"]) == EXIT_OK

    text = tmp_ioc.read_text(encoding="utf-8")
    assert "Dma.Request0=USART1_RX" in text
    assert "Dma.Request1=USART2_RX" in text
    assert "Dma.Request2=USART2_RX" not in text
    assert "Dma.RequestsNb=2" in text
    assert "Dma.USART2_RX.1.Instance=" in text
    assert "Dma.USART2_RX.2." not in text
    # 摘掉的只有 DMA 那个请求，**引脚上的 Signal 不动** —— PA9 仍然是 USART1_TX。
    # 这条分界很容易搞混：`Dma.USART1_TX.*` 是"这个串口用了 DMA 发"，
    # `PA9.Signal=USART1_TX` 是"这个脚接的是串口发"。删前者不能动后者。
    assert not [ln for ln in text.splitlines()
                if ln.startswith("Dma.USART1_TX.")]
    assert "PA9.Signal=USART1_TX" in text


def test_removing_the_last_dma_request_takes_the_whole_block(
    tmp_ioc: Path, capsys
) -> None:
    """删空之后删键而不是写 0，DMA 也从 `Mcu.IP*` 退场。

    实测 524 份 ST 官方板级文件里 `RequestsNb` 取过 1/2/3/6/7/9，**0 次都没出现过**：
    没有 DMA 的工程压根没有 `Dma.*` 键。
    """
    assert main(["remove-dma", str(tmp_ioc), "USART1_RX"]) == EXIT_OK
    assert main(["remove-dma", str(tmp_ioc), "USART1_TX"]) == EXIT_OK

    text = tmp_ioc.read_text(encoding="utf-8")
    assert "Dma." not in text
    assert "Mcu.IP0=DMA" not in text


def test_a_dma_command_that_cannot_use_the_db_is_a_usage_error(
    tmp_ioc: Path, capsys, monkeypatch
) -> None:
    """没有数据库时**不静默降级**：说不出这个请求合法与否，就不改。"""
    monkeypatch.setattr("cubemx_mcp.cli.config.db_root", lambda: None)
    before = tmp_ioc.read_bytes()

    assert main(["add-dma", str(tmp_ioc), "USART2_RX"]) == EXIT_USAGE

    assert "CubeMX 数据库" in capsys.readouterr().err
    assert tmp_ioc.read_bytes() == before


# ========================================================== 往返验证的接线


def _stub_verify(
    monkeypatch, *, lost: tuple[tuple[str, str], ...] = (), failure: str | None = None
) -> None:
    """把 `cubemx.verify` 换掉，只测 **CLI 的接线**（退出码、写不写盘）。

    真的起一次 CubeMX 要十几秒、还得本机装了它 —— 那是 `test_verify.py` 用假
    runner 覆盖的部分。这里要钉的是另一件事：**验证没过时，磁盘必须一个字节不变**。
    """

    def stub(project, **kwargs):  # noqa: ANN001, ANN202 - 只关心形状
        if failure is not None:
            return cubemx.Verification(failure=failure)
        return cubemx.Verification(cubemx.RoundTrip(lost_ours=lost))

    monkeypatch.setattr(cubemx, "verify", stub)


def test_verify_refuses_to_write_when_cubemx_drops_our_key(
    tmp_ioc: Path, capsys, monkeypatch
) -> None:
    """**这是本步的核心保证。** CubeMX 不认我们写的键 → 那就不写了。

    实测场景：给一个没配好引脚的外设开中断，CubeMX 加载时会把
    外设**连同那行 `NVIC.<IRQn>`** 一起悄悄删掉，不报任何错。`validate` 抓不到它
    （只看文件自己是不矛盾的），只有这一层能抓。
    """
    _stub_verify(monkeypatch, lost=(("NVIC.USART1_IRQn", "true:5:0"),))
    before = tmp_ioc.read_bytes()

    assert main(["enable-irq", str(tmp_ioc), "USART1_IRQn",
                 "--preemption", "5", "--verify"]) == EXIT_INVALID

    err = capsys.readouterr().err
    assert "本次写入的" in err and "NVIC.USART1_IRQn" in err
    assert "没有写入文件" in err
    assert tmp_ioc.read_bytes() == before
    assert not list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))


def test_verify_writes_when_the_round_trip_is_clean(
    tmp_ioc: Path, capsys, monkeypatch
) -> None:
    _stub_verify(monkeypatch)

    assert main(["enable-irq", str(tmp_ioc), "USART1_IRQn",
                 "--preemption", "5", "--verify"]) == EXIT_OK

    assert "一个键都没丢" in capsys.readouterr().err
    assert "NVIC.USART1_IRQn=true\\:5\\:0" in tmp_ioc.read_text(encoding="utf-8")


def test_a_verify_that_could_not_run_never_writes(
    tmp_ioc: Path, capsys, monkeypatch
) -> None:
    """**没验成 ≠ 验过了。** 没装 CubeMX / 超时 → 退出码 1，且照样不写盘。

    归 1 而不是 2 是刻意的：2 是"你这个工程有问题"，会让 Agent 去修一个根本没坏的
    东西。这里是"没验成"，属于命令跑不起来的范畴。
    """
    _stub_verify(monkeypatch, failure="找不到 CubeMX")
    before = tmp_ioc.read_bytes()

    # 优先级要真的改掉 —— 否则就是空改动，压根走不到验证那一步
    assert main(["enable-irq", str(tmp_ioc), "USART1_IRQn",
                 "--preemption", "5", "--verify"]) == EXIT_USAGE

    assert "没跑成" in capsys.readouterr().err
    assert tmp_ioc.read_bytes() == before


def test_dry_run_with_verify_reports_the_verdict_without_writing(
    tmp_ioc: Path, capsys, monkeypatch
) -> None:
    """`--dry-run --verify`：写不写之前先问一次"CubeMX 会不会吃掉这次改动"。

    退出码要跟着走 —— 这个组合的全部用途就是让调用方**不看报告也能判断**。
    """
    _stub_verify(monkeypatch, lost=(("NVIC.USART1_IRQn", "true:5:0"),))
    before = tmp_ioc.read_bytes()

    assert main(["enable-irq", str(tmp_ioc), "USART1_IRQn",
                 "--preemption", "5", "--dry-run", "--verify"]) == EXIT_INVALID

    captured = capsys.readouterr()
    assert "本次写入的" in captured.err
    assert "没有写入文件" in captured.out
    assert tmp_ioc.read_bytes() == before


def test_verify_command_is_read_only(tmp_ioc: Path, capsys, monkeypatch) -> None:
    """`verify` 自己不改文件 —— 连 `--dry-run` 都不用给。"""
    _stub_verify(monkeypatch)
    before = tmp_ioc.read_bytes()

    assert main(["verify", str(tmp_ioc)]) == EXIT_OK

    assert "一个键都没丢" in capsys.readouterr().out
    assert tmp_ioc.read_bytes() == before
    assert not list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))


def test_verify_command_json_shape(tmp_ioc: Path, capsys, monkeypatch) -> None:
    """没有 baseline：独立的 `verify` 问的是"这份工程 CubeMX 收不收"，
    所以任何键丢了都算数（不区分是不是"我们写的"）。"""
    _stub_verify(monkeypatch, lost=(("USART1.VirtualMode", "VM_ASYNC"),))

    assert main(["verify", str(tmp_ioc), "--json"]) == EXIT_INVALID

    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True and payload["ok"] is False
    assert payload["lost_ours"] == [{"key": "USART1.VirtualMode", "value": "VM_ASYNC"}]
    assert payload["notes"] == []


def test_verify_command_without_cubemx_says_so_and_exits_1(
    tmp_ioc: Path, capsys, monkeypatch
) -> None:
    """走**真的** `cubemx.verify`，只是把 exe 掐掉 —— 覆盖"没装 CubeMX"那条真实分支。"""
    monkeypatch.setattr("cubemx_mcp.cli.config.cube_mx_exe", lambda: None)

    assert main(["verify", str(tmp_ioc)]) == EXIT_USAGE

    assert "需要本机装有 STM32CubeMX" in capsys.readouterr().err


def test_verify_command_json_reports_a_run_that_never_happened(
    tmp_ioc: Path, capsys, monkeypatch
) -> None:
    """JSON 模式下"没跑成"也必须能机器读出来 —— 不能只打一句给人看的话。"""
    monkeypatch.setattr("cubemx_mcp.cli.config.cube_mx_exe", lambda: None)

    assert main(["verify", str(tmp_ioc), "--json"]) == EXIT_USAGE

    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False and payload["ok"] is False
    assert "STM32CubeMX" in payload["failure"]


