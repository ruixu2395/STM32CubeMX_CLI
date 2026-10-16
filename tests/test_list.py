"""``cubemx-cli list`` —— CubeMX 式的配置浏览（只读）。

样本是 `tests/data/Usb_uart.ioc`：STM32F407ZGT6，USART1 配了异步模式 + 两个 DMA
请求 + 三个中断。**三样配套齐全**，所以五个页都能验，不是只能验个模式名。

除了一般的"输出对不对"，这里还有一组**只读断言**：`list` 是这个工具里唯一
承诺不写盘的命令，得真的钉住 —— 字节不变、也不留备份文件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cubemx_mcp.cli import EXIT_OK, EXIT_USAGE, main
from cubemx_mcp.db import Device
from cubemx_mcp.ioc.project import IocProject


@pytest.fixture
def db_ready(device: Device) -> None:
    """数据库可用才往下跑。

    `list` 由 ``main()`` 自己开工程，拿不到 fixture，所以这里**借** `device`
    的跳过逻辑（没有数据库时它会 skip）—— 数据库不在和代码写错是两回事。
    """
    return None


@pytest.fixture
def bare_db(tmp_path: Path, monkeypatch) -> None:
    """把数据库彻底藏起来：环境变量指到不存在的地方，自动探测也没路标。

    三个都要设：``ST_CUBEMX_DB`` 指错会被忽略并**继续往下找**（这是刻意的，
    见 `resolve_db_root`），所以还得堵掉配置文件和 exe 探测。
    """
    monkeypatch.setenv("ST_CUBEMX_DB", str(tmp_path / "no-db"))
    monkeypatch.setenv("ST_CUBEMX_EXE", str(tmp_path / "no-CubeMX.exe"))
    monkeypatch.setenv("CUBEMX_CLI_CONFIG", str(tmp_path / "no-config.json"))


# ============================================================== 顶层清单


def test_list_shows_configured_and_unconfigured(tmp_ioc: Path, db_ready, capsys):
    assert main(["list", str(tmp_ioc)]) == EXIT_OK
    out = capsys.readouterr().out

    assert "STM32F407Z(E-G)Tx" in out
    assert "已配置" in out and "未配置" in out
    # USART1 在工程里配了，USART2 没有 —— 两个都得出现，只是分段不同
    assert "USART1" in out and "USART2" in out

    configured = out.split("已配置", 1)[1].split("未配置", 1)[0]
    assert "USART1" in configured
    assert "USART2" not in configured


def test_list_json_gives_the_mcu_and_both_groups(tmp_ioc: Path, db_ready, capsys):
    assert main(["list", str(tmp_ioc), "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)

    assert data["mcu"] == "STM32F407Z(E-G)Tx"
    assert data["complete"] is True
    # 样本里配了 4 个：USART1 加 CubeMX 自己总会写的 DMA/NVIC/RCC
    assert [i["name"] for i in data["configured"]] == ["DMA", "NVIC", "RCC", "USART1"]
    # 未配置的只给名字：带上 pins/params 那些字段会**全是空值**，纯噪声。
    # 两段加起来是这颗 MCU 的全部外设，一个不漏也不重。
    assert "USART2" in data["unconfigured"]
    assert len(data["configured"]) + len(data["unconfigured"]) == 58


def test_without_a_database_it_says_the_list_is_incomplete(
    tmp_ioc: Path, bare_db, capsys
):
    """**"没查"不等于"查了没问题"。**

    没数据库时清单里只有工程自己声明过的外设，绝不能让人看成"这颗 MCU 就这么多"——
    所以既要 ``complete=false``，也要在 stderr 上明说（stdout 要留给 `--json`）。
    """
    assert main(["list", str(tmp_ioc), "--json"]) == EXIT_OK
    captured = capsys.readouterr()

    assert json.loads(captured.out)["complete"] is False
    assert "只有工程里已配置的" in captured.err or "数据库" in captured.err


# ============================================================== 外设页


def test_inspect_renders_all_five_pages(tmp_ioc: Path, db_ready, capsys):
    assert main(["list", str(tmp_ioc), "USART1"]) == EXIT_OK
    out = capsys.readouterr().out

    for page in ("[模式]", "[参数]", "[中断 NVIC]", "[DMA 请求]", "[引脚 GPIO]"):
        assert page in out
    assert "Asynchronous" in out and "PA9" in out and "PA10" in out


def test_nvic_page_includes_the_stream_interrupts(tmp_ioc: Path, db_ready, capsys):
    """**这条是 `for_stream` 那条路。**

    配了 DMA 的 USART1，NVIC 页里除了自己的 ``USART1_IRQn`` 还必须有它两个 DMA
    请求各自的 stream 中断 —— 少了这两条，用户会以为 DMA 中断还没开。
    """
    assert main(["list", str(tmp_ioc), "USART1", "--page", "nvic"]) == EXIT_OK
    out = capsys.readouterr().out

    assert "USART1_IRQn" in out
    assert "DMA2_Stream2_IRQn" in out and "DMA2_Stream7_IRQn" in out
    assert "NVIC_PRIORITYGROUP_4" in out
    # 这三条样本里都是 true:0:0:…，所以都该报"已启用"
    assert out.count("已启用") == 3
    assert "[DMA 请求]" not in out  # --page 只给那一页


def test_dma_page_shows_the_current_stream_and_the_alternatives(
    tmp_ioc: Path, db_ready, capsys
):
    """要同时给出"现在挂的是哪个"和"还能换成哪些" —— 换 stream 是常见需求。"""
    assert main(["list", str(tmp_ioc), "USART1", "--page", "dma"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "USART1_RX" in out and "已配 DMA2_Stream2" in out
    assert "DMA2_Stream5" in out  # 备选
    # 固定项标出来，别让用户以为 Channel 能改
    assert "DMA_CHANNEL_4（固定）" in out


def test_dma_page_json_matches_whats_in_the_project(tmp_ioc: Path, db_ready, capsys):
    assert main(["list", str(tmp_ioc), "USART1", "--page", "dma", "--json"]) == EXIT_OK
    rows = {r["request"]: r for r in json.loads(capsys.readouterr().out)["dma"]}

    assert set(rows) == {"USART1_RX", "USART1_TX"}
    # 和文件里的 Dma.USART1_RX.0.Instance=DMA2_Stream2 一致
    assert rows["USART1_RX"]["current"] == "DMA2_Stream2"
    assert rows["USART1_RX"]["streams"] == ["DMA2_Stream2", "DMA2_Stream5"]
    assert rows["USART1_TX"]["current"] == "DMA2_Stream7"


def test_gpio_page_lists_what_is_settable(tmp_ioc: Path, db_ready, capsys):
    """派生项（``GPIOx``/``GPIO_Pin``）不给用户改，``GPIO_Label`` 得在。

    实测 505 份 ST 官方板级 ``.ioc``：``GPIO_Mode``/``GPIO_PuPd``/``GPIO_Speed``/
    ``GPIO_Label`` 是真正会被写进文件的四个，所以"可设"里就该是这四个。
    """
    assert main(["list", str(tmp_ioc), "USART1", "--page", "gpio", "--json"]) == EXIT_OK
    pins = {p["pin"]: p for p in json.loads(capsys.readouterr().out)["pins"]}

    assert set(pins) == {"PA9", "PA10"}
    for p in pins.values():
        assert "GPIO_Label" in p["settable"]
        assert "GPIOx" not in p["settable"] and "GPIO_Pin" not in p["settable"]
        assert "GPIO_PULLUP" in p["options"]["GPIO_PuPd"]["values"]


def test_gpio_ip_page_lists_plain_gpio_pins_with_options(tmp_ioc: Path, db_ready, capsys):
    """``list <工程> GPIO``：GPIO 脚没有归属外设，以前一个都列不出来。
    EXTI 的触发沿键 ``GPIO_ModeDefaultEXTI`` 必须在可设清单里、带可选值。"""
    assert main(["add-gpio", str(tmp_ioc), "PE3", "--mode", "EXTI",
                 "--set", "GPIO_ModeDefaultEXTI=GPIO_MODE_IT_FALLING"]) == EXIT_OK
    capsys.readouterr()
    assert main(["list", str(tmp_ioc), "GPIO", "--page", "gpio", "--json"]) == EXIT_OK
    pins = {p["pin"]: p for p in json.loads(capsys.readouterr().out)["pins"]}

    assert "PE3" in pins and "PA9" not in pins
    pe3 = pins["PE3"]
    assert pe3["params"]["GPIO_ModeDefaultEXTI"] == "GPIO_MODE_IT_FALLING"
    assert "GPIO_MODE_IT_FALLING" in pe3["options"]["GPIO_ModeDefaultEXTI"]["values"]


def test_param_page_uses_the_unconditional_default(tmp_ioc: Path, db_ready, capsys):
    """**这条是回归。** 同一个参数常被声明多次、各带一个 ``<Condition>``。

    ``Parity`` 的第一条声明是 SmartCard 模式下的 ``PARITY_EVEN``，``HwFlowCtl``
    的第一条是只收模式下的 ``RTS`` —— 按"先见先得"合并就会报出这两个假默认值。
    真默认值只在不带条件的那条里。
    """
    assert main(["list", str(tmp_ioc), "USART1", "--page", "param", "--json"]) == EXIT_OK
    params = {p["name"]: p for p in json.loads(capsys.readouterr().out)["params"]}

    assert params["Parity"]["default"] == "PARITY_NONE"
    assert params["HwFlowCtl"]["default"] == "UART_HWCONTROL_NONE"
    assert params["BaudRate"]["default"] == "115200"


def test_unset_params_are_still_listed(tmp_ioc: Path, db_ready, capsys):
    """**这正是这个命令的价值**：用户要看见"还有哪些能调"，不只是已经设了的。

    样本里 ``USART1.IPParameters=VirtualMode``，其余全没设 —— 但都得列出来。
    """
    assert main(["list", str(tmp_ioc), "USART1", "--page", "param", "--json"]) == EXIT_OK
    params = {p["name"]: p for p in json.loads(capsys.readouterr().out)["params"]}

    assert len(params) >= 6
    assert params["BaudRate"]["set"] is False
    assert params["WordLength"]["values"] == ["WORDLENGTH_8B", "WORDLENGTH_9B"]
    assert params["Parity"]["values"]  # 枚举值要带上，AI 才好挑


# ============================================================== 用法错误


def test_page_without_a_peripheral_is_a_usage_error(tmp_ioc: Path, db_ready, capsys):
    """``--page`` 只在外设页有意义 —— 静默忽略会让人以为过滤生效了。"""
    assert main(["list", str(tmp_ioc), "--page", "nvic"]) == EXIT_USAGE
    assert "--page 要指定外设" in capsys.readouterr().err


def test_an_unknown_page_is_a_usage_error(tmp_ioc: Path, db_ready, capsys):
    """argparse 的 ``choices`` 拦住它；退出码统一重映射成 1。"""
    assert main(["list", str(tmp_ioc), "USART1", "--page", "nonsense"]) == EXIT_USAGE
    assert "nonsense" in capsys.readouterr().err


def test_an_unknown_peripheral_is_rejected(tmp_ioc: Path, db_ready, capsys):
    """名字查不到就报错，**不能**当成"这外设没配"静默给个空页。"""
    assert main(["list", str(tmp_ioc), "NOSUCH"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "没有外设 NOSUCH" in err
    # 完全不像的名字本来就不该硬凑候选 —— 凑出来的建议比没有更糟
    assert "相近的有" not in err


def test_a_typo_gets_close_name_suggestions(tmp_ioc: Path, db_ready, capsys):
    """**这条是回归。** 原来只按 3 字符前缀找候选，于是最常见的错法
    （字母换位）恰好一条建议都给不出来：``USRT1`` 的前缀是 ``USR``，
    和 ``USA`` 对不上，用户只看到"没有这个外设"。
    """
    assert main(["list", str(tmp_ioc), "USRT1"]) == EXIT_USAGE
    assert "USART1" in capsys.readouterr().err


def test_a_bad_prefix_still_gets_candidates(tmp_ioc: Path, db_ready, capsys):
    """前缀那一档也要留着：``USART9`` 该给出 USART1..6 这一族。"""
    assert main(["list", str(tmp_ioc), "USART9"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "相近的有" in err and "USART1" in err


# ============================================================== 只读承诺


def test_list_never_touches_the_file(tmp_ioc: Path, db_ready, capsys):
    """**整个工具敢让 AI 直接调的前提。**

    `list` 是只读命令：跑完磁盘上的字节要一模一样，而且**不能留下备份** ——
    写命令才该有 ``.bak-*``，浏览命令造出一堆备份会让人以为改过东西。
    """
    before = tmp_ioc.read_bytes()
    siblings_before = {p.name for p in tmp_ioc.parent.iterdir()}

    assert main(["list", str(tmp_ioc)]) == EXIT_OK
    assert main(["list", str(tmp_ioc), "USART1"]) == EXIT_OK
    assert main(["list", str(tmp_ioc), "USART1", "--page", "nvic", "--json"]) == EXIT_OK
    capsys.readouterr()

    assert tmp_ioc.read_bytes() == before
    after = {p.name for p in tmp_ioc.parent.iterdir()}
    assert after == siblings_before
    assert not [n for n in after if ".bak" in n]


def test_json_stdout_is_pure_json(tmp_ioc: Path, db_ready, capsys):
    """stdout 只有 JSON，提示语一律走 stderr —— 否则 AI 没法直接 ``json.loads``。"""
    assert main(["list", str(tmp_ioc), "USART1", "--json"]) == EXIT_OK
    captured = capsys.readouterr()

    data = json.loads(captured.out)  # 混了别的东西这里就炸
    assert data["ip"] == "USART1" and data["kind"] == "USART"
    assert data["nvic"]["irqs"] and data["dma"] and data["pins"]


# ========================================================== 看不了某一页


@pytest.fixture
def h5_ioc(tmp_path: Path, db_ready, device: Device) -> Path:
    """一颗**没有 NVIC / DMA 表**的 MCU 的工程。

    数据库里 2123 颗 MCU 只有 69 份 ``NVIC-*.xml``，所以"这一页看不了"是**常见
    状态**，不是异常。用 STM32H523（有 USART1 和模式库，但没有中断/DMA 表）。
    """
    try:
        Device.open("STM32H523CCTx")
    except Exception as exc:  # noqa: BLE001 - 数据库布局变化时给出可读原因
        pytest.skip(f"数据库里没有 STM32H523CCTx：{exc}")

    p = tmp_path / "H5.ioc"
    p.write_text(
        "#MicroXplorer Configuration settings - do not modify\n"
        "Mcu.Name=STM32H523CCTx\n"
        "Mcu.UserName=STM32H523CCTx\n",
        encoding="utf-8",
    )
    return p


def test_an_unreadable_page_is_reported_not_shown_as_empty(h5_ioc: Path, capsys):
    """**"没查"不等于"查了没问题"** —— 这是整个工具最在意的一条纪律。

    中断表和 DMA 表读不出来时，页里必须是 ``null`` + ``missing`` 点名，
    绝不能给个空列表让 AI 转述成"这个外设没有中断"。
    """
    assert main(["list", str(h5_ioc), "USART1", "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)

    # null 而不是 [] —— 空列表会被读成"这个外设没有中断"
    assert data["nvic"] is None and data["dma"] is None
    # missing 里是**页名**，`--json` 的调用方能直接 `"nvic" in missing`
    assert set(data["missing"]) >= {"nvic", "dma"}
    # 读得出来的页照常给 —— 一页看不了不该把别的页也拖下水
    assert data["modes"] and data["params"]


def test_an_unreadable_page_says_so_in_text_too(h5_ioc: Path, capsys):
    assert main(["list", str(h5_ioc), "USART1"]) == EXIT_OK
    out = capsys.readouterr().out

    assert "看不了" in out and "中断表" in out and "DMA 请求表" in out
    # 整页不印，而不是印成"没有中断"/"没有 DMA 请求"——那是把"没查"说成结论
    assert "[中断 NVIC]" not in out and "[DMA 请求]" not in out
    assert "还可以开" not in out and "没有 DMA 请求" not in out
    # 读得出来的页还在
    assert "[模式]" in out


# ========================================================= 内部不变量


def test_only_irqn_suffixed_keys_count_as_interrupts(tmp_path: Path):
    """``NVIC.<名>`` 里只有带 ``IRQn`` 后缀的才是中断开关。

    **这条直接钉内部函数**，因为它没有可观察的外在表现：页里的行是数据库驱动的，
    计数也要在库里查得到，所以一个混进来的标量键今天既不会多出一行也不会多算一个。
    留着它是因为"没错"靠的是巧合 —— 换成 `PriorityGroup` 那种白名单，CubeMX 加一个
    新标量键（``TimeBase`` 就是）就会静默把它当前中断。

    后缀这条规则是**全库验证过**的：69 份 NVIC 文件、5141 条中断，无一例外。
    标量那侧实测有 8 种：``PriorityGroup`` / ``ForceEnableDMAVector`` / ``TimeBase`` /
    ``TimeBaseIP`` / ``BFHFNMINS`` / ``PRIS`` / ``Saved{X}IrqHandlerGenerated``。
    """
    from cubemx_mcp import ops  # 内部不变量，故意不经过命令行

    p = tmp_path / "Scalars.ioc"
    p.write_text(
        "#MicroXplorer Configuration settings - do not modify\n"
        "Mcu.IP0=NVIC\n"
        "NVIC.BFHFNMINS=0\n"
        "NVIC.PRIS=1\n"
        "NVIC.PriorityGroup=NVIC_PRIORITYGROUP_4\n"
        "NVIC.SavedPendsvIrqHandlerGenerated=true\n"
        "NVIC.TimeBase=TIM6_DAC_IRQn\n"
        "NVIC.TimeBaseIP=TIM6\n"
        "NVIC.USART1_IRQn=true\\:0\\:0\\:false\\:false\\:true\\:true\\:true\\:true\n",
        encoding="utf-8",
    )
    state = ops._nvic_state(IocProject.load(p))

    assert set(state) == {"USART1_IRQn"}
    assert state["USART1_IRQn"] == (True, 0, 0)


def test_a_truncated_nvic_line_does_not_crash(tmp_path: Path):
    """手写的残缺行也认：缺段按"没设"处理，浏览命令不该因一行畸形数据整条挂掉。"""
    from cubemx_mcp import ops

    p = tmp_path / "Broken.ioc"
    p.write_text(
        "#MicroXplorer Configuration settings - do not modify\n"
        "NVIC.USART1_IRQn=true\n"
        "NVIC.USART2_IRQn=true\\:5\n",
        encoding="utf-8",
    )
    state = ops._nvic_state(IocProject.load(p))

    assert state["USART1_IRQn"] == (True, None, None)
    assert state["USART2_IRQn"] == (True, 5, None)


def test_json_only_carries_the_requested_page(tmp_ioc: Path, db_ready, capsys):
    """``--page`` 给了就只算那页 —— 省掉无谓的数据库加载，也不给多余的东西。

    没点到的页是 ``null``（不是 ``[]``）：``[]`` 的含义是**问了且确实是空的**，
    和"没问"混在一起就会让人把没查过的页当成查过没问题的页。
    """
    assert main(["list", str(tmp_ioc), "USART1", "--page", "nvic", "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)

    assert data["nvic"]["irqs"]
    assert data["dma"] is None and data["pins"] is None and data["params"] is None
    assert data["missing"] == []  # 这页读得出来，所以它不在 missing 里
