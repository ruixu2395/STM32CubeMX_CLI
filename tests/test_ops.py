"""`cubemx_mcp.ops` —— 编辑操作。

分两档测：

* **逐字段**（`set_pin` / `clear_pin` / `set_peripheral` / `add_gpio`）
  不依赖数据库，用 `project` 夹具。
* **意图级**（`add_peripheral` / `list_modes`）依赖数据库，用 `db_project`。
  没有数据库的环境会自动跳过（见 `conftest.device`）。

意图级断言里的引脚（USART2 TX→PA2/RX→PA3、SPI2 用 `VirtualType`、
TIM3 只吃虚拟信号）都是查 F407 的库得到的**事实**，不是猜的。
"""

from __future__ import annotations

import pytest

from cubemx_mcp import ops
from cubemx_mcp.ioc.project import IocProject, Pin
from cubemx_mcp.ops import OpError


# =============================================================== resolve_pin


def test_resolve_pin_completes_name_from_db(db_project: IocProject):
    """库里 PA0 叫 PA0-WKUP、PC13 叫 PC13-ANTI_TAMP，用户报裸名要能对上。"""
    assert ops.resolve_pin(db_project, "PC13") == "PC13-ANTI_TAMP"
    assert ops.resolve_pin(db_project, "PA0") == "PA0-WKUP"
    assert ops.resolve_pin(db_project, "PA9") == "PA9"
    assert ops.resolve_pin(db_project, " pa2 ") == "PA2"


def test_resolve_pin_without_db_only_normalizes(project: IocProject):
    assert ops.resolve_pin(project, "pa9") == "PA9"


def test_resolve_pin_leaves_virtual_pins_alone(project: IocProject):
    assert ops.resolve_pin(project, "VP_SYS_VS_Systick") == "VP_SYS_VS_Systick"


def test_resolve_pin_rejects_garbage(project: IocProject):
    with pytest.raises(ValueError):
        ops.resolve_pin(project, "hello")


# =================================================================== set_pin


def test_set_pin_configures_and_registers_owner(project: IocProject):
    changes = ops.set_pin(project, "PA2", signal="USART2_TX", mode="Asynchronous")

    assert project.doc.get("PA2.Signal") == "USART2_TX"
    assert project.doc.get("PA2.Mode") == "Asynchronous"
    # 归属的外设必须自动登记，否则 CubeMX 不会初始化它
    assert "USART2" in project.peripheral_names()
    assert any("USART2" in c for c in changes)


def test_set_pin_label_goes_through_the_registry(project: IocProject):
    ops.set_pin(project, "PB0", signal="GPIO_Output", label="LED")
    assert project.doc.get("PB0.GPIOParameters") == "GPIO_Label"
    assert project.doc.get("PB0.GPIO_Label") == "LED"


def test_set_pin_props_are_registered(project: IocProject):
    ops.set_pin(project, "PB0", signal="GPIO_Output",
                props={"GPIO_PuPd": "GPIO_NOPULL"})
    assert project.doc.get("PB0.GPIO_PuPd") == "GPIO_NOPULL"
    assert "GPIO_PuPd" in project.find_pin("PB0").parameters


def test_set_pin_refuses_to_steal_an_occupied_pin(project: IocProject):
    """PA9 属于 USART1，改成外设 USART2 必须显式 --force。"""
    with pytest.raises(OpError, match="USART1"):
        ops.set_pin(project, "PA9", signal="USART2_TX")
    assert project.doc.get("PA9.Signal") == "USART1_TX"


def test_set_pin_force_allows_the_steal(project: IocProject):
    ops.set_pin(project, "PA9", signal="USART2_TX", force=True)
    assert project.doc.get("PA9.Signal") == "USART2_TX"
    assert "USART2" in project.peripheral_names()


def test_set_pin_same_owner_is_not_a_conflict(project: IocProject):
    """同一个外设内部换脚不算抢占（USART1_TX -> USART1_RX 是用户自找的，但放行）。"""
    ops.set_pin(project, "PA9", signal="USART1_RX")
    assert project.doc.get("PA9.Signal") == "USART1_RX"


def test_set_pin_on_unknown_pin_without_config_errors(project: IocProject):
    with pytest.raises(OpError, match="PA2"):
        ops.set_pin(project, "PA2")


def test_set_pin_creates_the_pin_when_told_what_to_do(project: IocProject):
    ops.set_pin(project, "PA2", signal="USART2_TX")
    assert "PA2" in project.pin_names()


def test_set_pin_unlock(project: IocProject):
    ops.set_pin(project, "PA9", locked=False)
    assert project.doc.get("PA9.Locked") == "false"


# ================================================================= clear_pin


def test_clear_pin_keeps_the_peripheral_when_other_pins_remain(project: IocProject):
    changes = ops.clear_pin(project, "PA9")
    assert "PA9" not in project.pin_names()
    assert "USART1" in project.peripheral_names()
    assert project.doc.get("PA9.Signal") is None
    assert changes == ["移除引脚 PA9"]


def test_clear_pin_removes_the_orphan_peripheral(project: IocProject):
    """一个引脚都不剩的 USART1 是个半配置：CubeMX 会初始化一个没引脚的串口。"""
    ops.clear_pin(project, "PA9")
    changes = ops.clear_pin(project, "PA10")

    assert "USART1" not in project.peripheral_names()
    assert project.doc.get("USART1.VirtualMode") is None
    assert any("孤儿" in c for c in changes)
    # 连带丢掉的东西要说清楚，不能悄悄扔
    assert any("VirtualMode" in c for c in changes)


def test_clear_pin_on_unknown_pin(project: IocProject):
    with pytest.raises(OpError):
        ops.clear_pin(project, "PA2")


# ============================================================ set_peripheral


def test_set_peripheral_creates_and_registers(project: IocProject):
    ops.set_peripheral(project, "USART2",
                       virtual=("VirtualMode", "VM_ASYNC"),
                       params={"BaudRate": "115200"})

    assert "USART2" in project.peripheral_names()
    assert project.doc.get("USART2.VirtualMode") == "VM_ASYNC"
    assert set(project.find_peripheral("USART2").parameters) == {"VirtualMode", "BaudRate"}


def test_set_peripheral_on_unknown_ip_just_adds_it(project: IocProject):
    """逐字段命令不做"这颗 MCU 上有没有这个外设"的检查 —— 那是校验层的事。"""
    ops.set_peripheral(project, "UART7", virtual=("VirtualMode", "VM_ASYNC"))
    assert "UART7" in project.peripheral_names()


# ========================================================= remove_peripheral


def test_remove_peripheral_takes_the_pins_with_it(project: IocProject):
    changes = ops.remove_peripheral(project, "USART1")
    assert project.peripheral_names() == ("DMA", "NVIC", "RCC")
    assert "PA9" not in project.pin_names()
    assert any("连带移除引脚" in c for c in changes)


def test_remove_peripheral_keep_pins(project: IocProject):
    ops.remove_peripheral(project, "USART1", clear_pins=False)
    assert "PA9" in project.pin_names()
    assert project.doc.get("PA9.Signal") is None
    assert project.doc.get("PA9.Mode") is None


def test_remove_unknown_peripheral(project: IocProject):
    with pytest.raises(OpError, match="USART9"):
        ops.remove_peripheral(project, "USART9")


# =================================================================== add_gpio


def test_add_gpio_output_writes_signal_label_lock(project: IocProject):
    ops.add_gpio(project, "PB0", mode="Output", label="LED")

    assert project.doc.get("PB0.Signal") == "GPIO_Output"
    assert project.doc.get("PB0.GPIO_Label") == "LED"
    assert project.doc.get("PB0.GPIOParameters") == "GPIO_Label"
    assert project.doc.get("PB0.Locked") == "true"
    # 实测 CubeMX 生成的 GPIO 引脚**不写** Mode 键
    assert project.doc.get("PB0.Mode") is None


def test_add_gpio_exti_uses_the_gpxti_line(project: IocProject):
    """EXTI 的 signal 是 ``GPXTI<脚号>``，不是 ``GPIO_EXTI0``。"""
    ops.add_gpio(project, "PB0", mode="EXTI")
    assert project.doc.get("PB0.Signal") == "GPXTI0"


def test_add_gpio_input_and_analog(project: IocProject):
    ops.add_gpio(project, "PC0", mode="Input")
    ops.add_gpio(project, "PC1", mode="Analog")
    assert project.doc.get("PC0.Signal") == "GPIO_Input"
    assert project.doc.get("PC1.Signal") == "GPIO_Analog"


def test_add_gpio_replaces_a_stale_config(project: IocProject):
    """PB0 先被配成 Output 又改回 Input：旧标签必须跟着走，不能残留。"""
    ops.add_gpio(project, "PB0", mode="Output", label="LED")
    ops.add_gpio(project, "PB0", mode="Input")

    assert project.doc.get("PB0.Signal") == "GPIO_Input"
    assert project.doc.get("PB0.GPIO_Label") is None


def test_add_gpio_refuses_an_occupied_pin(project: IocProject):
    with pytest.raises(OpError, match="USART1"):
        ops.add_gpio(project, "PA9", mode="Input")


def test_add_gpio_unlock(project: IocProject):
    ops.add_gpio(project, "PB0", mode="Output", locked=False)
    assert project.doc.get("PB0.Locked") is None


def test_add_gpio_unknown_pin_without_db(project: IocProject):
    """没有数据库时无法判引脚能力，PZ0 会被照样写下去 —— 由校验层兜底。"""
    ops.add_gpio(project, "PZ0", mode="Output")
    assert project.doc.get("PZ0.Signal") == "GPIO_Output"


def test_add_gpio_unknown_pin_with_db_is_rejected(db_project: IocProject):
    with pytest.raises(OpError, match="PZ0"):
        ops.add_gpio(db_project, "PZ0", mode="Output")


# ================================================================ can_pin_do


def test_can_pin_do_detects_a_real_capability_gap(db_project: IocProject):
    """PA11 有 USART1_CTS 却**没有** USART1_RX —— 查表才看得出来。"""
    ok, why = ops.can_pin_do(db_project, "PA11", "USART1_RX")
    assert not ok
    assert "PA10" in why  # 给出真正能用的脚


def test_can_pin_do_allows_what_the_table_allows(db_project: IocProject):
    ok, why = ops.can_pin_do(db_project, "PA2", "USART2_TX")
    assert ok and why == ""


def test_can_pin_do_without_db_admits_it_cannot_check(project: IocProject):
    ok, why = ops.can_pin_do(project, "PA2", "USART2_TX")
    assert ok and "未校验" in why


# ============================================================ add_peripheral


def test_add_peripheral_picks_pins_and_mode(db_project: IocProject):
    """说"要 USART2"就够了：模式取库里的默认值，TX/RX 自动落到 PA2/PA3。"""
    result = ops.add_peripheral(db_project, "USART2")

    assert result.mode == "Asynchronous"
    assert result.virtual == ("VirtualMode", "VM_ASYNC")
    assert {a.signal: a.pin for a in result.allocations} == {"TX": "PA2", "RX": "PA3"}

    assert db_project.doc.get("PA2.Signal") == "USART2_TX"
    assert db_project.doc.get("PA2.Mode") == "Asynchronous"
    assert db_project.doc.get("PA2.Locked") == "true"
    assert db_project.doc.get("USART2.VirtualMode") == "VM_ASYNC"
    assert "USART2" in db_project.peripheral_names()


def test_add_peripheral_honours_explicit_pins(db_project: IocProject):
    result = ops.add_peripheral(db_project, "USART2", pins={"TX": "PD5", "RX": "PD6"})

    assert {a.signal: a.pin for a in result.allocations} == {"TX": "PD5", "RX": "PD6"}
    assert all(a.source == "指定" for a in result.allocations)
    assert db_project.doc.get("PD5.Signal") == "USART2_TX"


def test_add_peripheral_reuses_pins_already_in_the_project(db_project: IocProject):
    """样本里 USART1 已经在 PA9/PA10 上，再"加"一次不该另占两个脚。"""
    result = ops.add_peripheral(db_project, "USART1")

    assert {a.pin for a in result.allocations} == {"PA9", "PA10"}
    assert all(a.source == "已在工程里" for a in result.allocations)
    assert db_project.pin_names() == ("PA9", "PA10")


def test_add_peripheral_rejects_a_pin_that_cannot_do_the_job(db_project: IocProject):
    with pytest.raises(OpError, match="PA11"):
        ops.add_peripheral(db_project, "USART2", pins={"RX": "PA11", "TX": "PA2"})


def test_add_peripheral_rejects_an_occupied_pin(db_project: IocProject):
    with pytest.raises(OpError, match="PA9"):
        ops.add_peripheral(db_project, "USART2", pins={"TX": "PA9", "RX": "PA3"})


def test_add_peripheral_handles_virtual_signals(db_project: IocProject):
    """TIM3 的默认模式只要 ``VS_ClockSourceINT`` —— 一个真实引脚都不占。"""
    result = ops.add_peripheral(db_project, "TIM3")

    assert [a.virtual for a in result.allocations] == [True]
    assert result.allocations[0].pin is None
    assert "VP_TIM3_VS_ClockSourceINT" in db_project.pin_names()
    assert db_project.doc.get("VP_TIM3_VS_ClockSourceINT.Signal") == "TIM3_VS_ClockSourceINT"


def test_the_virtual_pin_mode_is_not_the_peripheral_mode(db_project: IocProject):
    """虚拟引脚的 ``Mode`` 写的是**声明了该 VS 信号的那个模式**，不是外设模式。

    TIM3 的默认模式是 ``Enable_Timer``，但 ``VS_ClockSourceINT`` 同时被
    ``Enable_Timer``（无 UserName 的内部伪模式）和 ``Internal``（GUI 里的
    "Internal Clock"）声明。实测 CubeMX 要的是后者，且原样保留 —— 写
    ``Enable_Timer`` 会被改写成 ``Internal``。漏写 ``Mode`` 更糟：TIM3 会整个
    从 ``Mcu.IP*`` 里消失，生成的代码不初始化它。
    """
    result = ops.add_peripheral(db_project, "TIM3")

    assert result.mode == "Enable_Timer"  # 外设模式照旧
    assert db_project.doc.get("VP_TIM3_VS_ClockSourceINT.Mode") == "Internal"
    assert "VP_TIM3_VS_ClockSourceINT.Mode = Internal" in result.changes


def test_add_peripheral_spi_uses_virtual_type(db_project: IocProject):
    """SPI 用 ``VirtualType`` 而不是 ``VirtualMode`` —— 两者不可混用。"""
    result = ops.add_peripheral(db_project, "SPI2")

    assert result.mode == "Full_Duplex_Master"
    assert result.virtual == ("VirtualType", "VM_MASTER")
    assert db_project.doc.get("SPI2.VirtualType") == "VM_MASTER"
    assert db_project.doc.get("SPI2.VirtualMode") is None


def test_add_peripheral_matches_signals_against_the_full_name(db_project: IocProject):
    """``pins_for("USART2","TX")`` 用前缀+尾段精确匹配，不能被 UART 家族串味。"""
    ops.add_peripheral(db_project, "USART2")
    assert db_project.doc.get("PA2.Signal") == "USART2_TX"
    assert db_project.doc.get("PA2.Signal") != "USART1_TX"


def test_add_peripheral_reports_pin_exhaustion_helpfully(db_project: IocProject):
    """把 USART2 的四个脚全占了，再要 USART2 就没有活路 —— 报错要说清楚。"""
    for pin in ("PA2", "PA3", "PD5", "PD6"):
        ops.add_gpio(db_project, pin, mode="Output")

    with pytest.raises(OpError) as exc:
        ops.add_peripheral(db_project, "USART2")

    msg = str(exc.value)
    assert "USART2_TX" in msg or "USART2_RX" in msg
    assert "--tx" in msg or "--rx" in msg  # 告诉用户显式指定的出路


def test_add_peripheral_accepts_params(db_project: IocProject):
    ops.add_peripheral(db_project, "USART2", params={"BaudRate": "9600",
                                                     "Parity": "PARITY_ODD"})
    assert db_project.doc.get("USART2.BaudRate") == "9600"
    assert db_project.doc.get("USART2.Parity") == "PARITY_ODD"
    assert "BaudRate" in db_project.find_peripheral("USART2").parameters


def test_add_peripheral_rejects_a_param_not_in_the_mode(db_project: IocProject):
    with pytest.raises(OpError, match="ClockPolarity"):
        ops.add_peripheral(db_project, "USART2", params={"ClockPolarity": "HIGH"})


def test_add_peripheral_rejects_a_bad_param_value(db_project: IocProject):
    with pytest.raises(OpError) as exc:
        ops.add_peripheral(db_project, "USART2", params={"Parity": "NONSENSE"})
    assert "PARITY_NONE" in str(exc.value)  # 列出合法取值


def test_add_peripheral_rejects_an_unknown_mode(db_project: IocProject):
    with pytest.raises(OpError, match="Asynchronous"):
        ops.add_peripheral(db_project, "USART2", mode="Nonsense")


def test_add_peripheral_rejects_an_unknown_peripheral(db_project: IocProject):
    with pytest.raises(OpError, match="USART9"):
        ops.add_peripheral(db_project, "USART9")


def test_add_peripheral_requires_the_database(project: IocProject):
    with pytest.raises(OpError, match="ST_CUBEMX_DB"):
        ops.add_peripheral(project, "USART2")


def test_add_peripheral_failure_leaves_the_project_untouched(db_project: IocProject):
    """前面的检查全部跑完才动工程 —— 失败时不能留下半个配置。"""
    before = db_project.render()
    with pytest.raises(OpError):
        ops.add_peripheral(db_project, "USART2", pins={"RX": "PA11"})
    assert db_project.render() == before


# ================================================================= list_modes


def test_list_modes_gives_name_virtual_and_signals(db_project: IocProject):
    rows = dict((name, (virt, sigs)) for name, virt, sigs in ops.list_modes(db_project, "USART2"))

    assert rows["Asynchronous"] == ("VirtualMode=VM_ASYNC", ("RX", "TX"))
    assert rows["Synchronous"][1] == ("CK", "RX", "TX")
    assert "Half_duplex(single_wire_mode)" in rows


def test_list_modes_needs_the_database(project: IocProject):
    with pytest.raises(OpError):
        ops.list_modes(project, "USART2")


# ================================================================ 综合场景


def test_a_whole_project_can_be_built_from_intent(db_project: IocProject):
    """一条条说意图，最后落一份自洽的工程 —— 这是本工具的用法。"""
    ops.add_peripheral(db_project, "USART2", params={"BaudRate": "115200"})
    ops.add_peripheral(db_project, "SPI2")
    ops.add_peripheral(db_project, "I2C1")
    ops.add_peripheral(db_project, "TIM3")
    ops.add_gpio(db_project, "PC13", mode="Output", label="KEY")
    ops.add_gpio(db_project, "PB0", mode="EXTI")

    assert db_project.peripheral_names() == (
        "DMA", "I2C1", "NVIC", "RCC", "SPI2", "TIM3", "USART1", "USART2",
    )
    assert db_project.doc.get("PC13-ANTI_TAMP.GPIO_Label") == "KEY"
    assert db_project.doc.get("PB0.Signal") == "GPXTI0"

    # 引脚清单按封装位号单调递增，虚拟引脚（查不到位号）恒排最后
    names = db_project.pin_names()
    assert names[0] == "PC13-ANTI_TAMP"          # 位号 7，全 MCU 最小的几个之一
    assert names[-1] == "VP_TIM3_VS_ClockSourceINT"
    real = [n for n in names if not n.startswith("VP_")]
    positions = [db_project.device.mcu.pins[n].position for n in real]
    assert positions == sorted(positions)
