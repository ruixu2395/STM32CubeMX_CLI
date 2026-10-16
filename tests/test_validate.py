"""`cubemx_mcp.validate` —— 五层校验。

**这一层是唯一的防线。** CubeMX 对 ``config load`` → ``saveas`` 不做一致性检查，
半配置的工程会被静默接受并生成不初始化外设的代码。所以每条规则都要有测试。

大部分用例是"手工构造一个恰好错一处"的工程，然后断言校验器**只**报出那一处 ——
只报对的还不够，不误报同样重要。
"""

from __future__ import annotations

import pytest

from cubemx_mcp import ops, validate
from cubemx_mcp.ioc.project import IocProject, Pin


def codes(report: validate.Report) -> set[str]:
    return {i.code for i in report.issues}


def issue(report: validate.Report, code: str) -> validate.Issue:
    (found,) = [i for i in report.issues if i.code == code]
    return found


# ============================================================ 干净的工程


def test_a_real_project_passes_clean(db_project: IocProject):
    report = validate.validate(db_project)
    assert report.issues == [], report.render()
    assert report.skipped == []
    assert report.ok
    assert "通过" in report.render()


def test_building_a_project_by_intent_keeps_it_valid(db_project: IocProject):
    """工具写出来的东西必须自己就能过校验 —— 否则它就是个错误制造机。"""
    ops.add_peripheral(db_project, "USART2", params={"BaudRate": "115200"})
    ops.add_peripheral(db_project, "SPI2")
    ops.add_peripheral(db_project, "TIM3")
    ops.add_gpio(db_project, "PC13", mode="Output", label="KEY")
    ops.add_gpio(db_project, "PB0", mode="EXTI")

    report = validate.validate(db_project)
    assert report.errors == [], report.render()


# ==================================================== 第 1 层：引脚能力


def test_unknown_pin(db_project: IocProject):
    db_project.add_pin("PZ0")
    db_project.find_pin("PZ0").signal = "GPIO_Output"

    report = validate.validate(db_project)
    assert issue(report, "pin.unknown").subject == "PZ0"
    assert "STM32F407Z(E-G)Tx" in issue(report, "pin.unknown").message


def test_signal_the_pin_cannot_carry(db_project: IocProject):
    """PA11 有 ``USART1_CTS`` 却**没有** ``USART1_RX``。

    光看 signal 名看不出这一点，只有查 ``db/mcu`` 的引脚表才知道 —— 这正是
    第 1 层存在的理由。
    """
    db_project.add_pin("PA11")
    pin = db_project.find_pin("PA11")
    pin.signal = "USART1_RX"
    pin.mode = "Asynchronous"

    report = validate.validate(db_project)
    found = issue(report, "pin.unsupported_signal")
    assert found.subject == "PA11"
    assert "PA10" in found.hint  # 点出真正能用的脚


def test_gpio_signal_capability_error_lists_the_available_modes(db_project: IocProject):
    db_project.add_pin("PA9")
    db_project.find_pin("PA9").mode = None
    db_project.find_pin("PA9").signal = "GPIO_NonexistentMode"

    report = validate.validate(db_project)
    assert "pin.unsupported_signal" in codes(report)


def test_virtual_pins_are_exempt_from_capability(db_project: IocProject):
    """``VP_TIM3_VS_ClockSourceINT`` 不在 MCU 引脚表里，不该报 pin.unknown。"""
    ops.add_peripheral(db_project, "TIM3")
    assert "pin.unknown" not in codes(validate.validate(db_project))


# ================================================ 第 2 层：归属与登记


def test_pin_whose_peripheral_is_not_registered(db_project: IocProject):
    """引脚有 Signal 而外设不在 Mcu.IP* —— CubeMX 不会初始化它。"""
    db_project.add_pin("PB0")
    pin = db_project.find_pin("PB0")
    pin.signal = "USART2_TX"
    pin.mode = "Asynchronous"

    report = validate.validate(db_project)
    found = issue(report, "ip.not_registered")
    assert found.level == "error"
    assert found.subject == "USART2"
    assert "USART2" in found.hint


def test_registered_peripheral_without_pins_is_a_warning(db_project: IocProject):
    db_project.add_peripheral("SPI2")
    found = issue(validate.validate(db_project), "ip.no_pins")
    assert found.level == "warning"  # 可能只是"先配参数再配引脚"
    assert found.subject == "SPI2"


def test_system_peripherals_are_exempt_from_the_pin_requirement(db_project: IocProject):
    """NVIC / RCC / DMA 恒在 Mcu.IP* 里却不接引脚，不该报 ip.no_pins。"""
    assert "ip.no_pins" not in codes(validate.validate(db_project))


def test_a_registered_peripheral_with_pins_is_fine(db_project: IocProject):
    assert "ip.not_registered" not in codes(validate.validate(db_project))
    assert "ip.no_pins" not in codes(validate.validate(db_project))


# ==================================================== 第 3 层：模式自洽


def test_mode_not_in_the_db(db_project: IocProject):
    db_project.doc.set("PA9.Mode", "Nonsense")
    found = issue(validate.validate(db_project), "mode.unknown")
    assert found.subject == "PA9"


def test_non_gpio_signal_without_a_mode(db_project: IocProject):
    """``Signal`` 有而 ``Mode`` 没有 —— CubeMX 不把这一脚算进外设初始化。"""
    db_project.doc.discard("PA10.Mode")

    found = issue(validate.validate(db_project), "mode.missing")
    assert found.subject == "PA10"
    assert "--mode" in found.hint


def test_gpio_pins_legitimately_have_no_mode(db_project: IocProject):
    """GPIO 引脚按 CubeMX 惯例不写 Mode —— 不能把它误判成 mode.missing。"""
    ops.add_gpio(db_project, "PC13", mode="Output", label="KEY")
    report = validate.validate(db_project)
    assert report.errors == [], report.render()


def test_inconsistent_modes_within_one_peripheral(db_project: IocProject):
    db_project.doc.set("PA10.Mode", "Synchronous")
    found = issue(validate.validate(db_project), "mode.inconsistent")
    assert found.subject == "USART1"
    assert "Asynchronous" in found.message and "Synchronous" in found.message


def test_missing_signals_for_the_mode(db_project: IocProject):
    """``Synchronous`` 要 CK+RX+TX，只给 TX/RX 是错的。"""
    db_project.doc.set("PA9.Mode", "Synchronous")
    db_project.doc.set("PA10.Mode", "Synchronous")

    found = issue(validate.validate(db_project), "mode.missing_signals")
    assert "CK" in found.message


def test_virtual_mode_missing(db_project: IocProject):
    """引脚是 Asynchronous，外设却少了 VirtualMode —— 两个键必须成对。"""
    db_project.doc.discard("USART1.VirtualMode")

    found = issue(validate.validate(db_project), "virtual.missing")
    assert found.subject == "USART1"
    assert "VirtualMode=VM_ASYNC" in found.message


def test_virtual_mode_mismatch(db_project: IocProject):
    db_project.doc.set("USART1.VirtualMode", "VM_SYNC")

    found = issue(validate.validate(db_project), "virtual.mismatch")
    assert "VM_ASYNC" in found.message and "VM_SYNC" in found.message


def test_unexpected_virtual_key_is_a_warning(db_project: IocProject):
    """I2C 没有"虚拟模式"这个概念，写了就是多余的。"""
    db_project.add_pin("PB6")
    pin = db_project.find_pin("PB6")
    pin.signal = "I2C1_SCL"
    pin.mode = "I2C"
    db_project.add_peripheral("I2C1")
    db_project.find_peripheral("I2C1").set_virtual("VirtualMode", "VM_WHATEVER")

    found = issue(validate.validate(db_project), "virtual.unexpected")
    assert found.level == "warning"
    assert "--unset" in found.hint


def test_missing_modes_file_is_reported_as_skipped_not_passed(db_project, monkeypatch):
    """数据库里没有这个外设的模式库时，要说"没查"，不能说"通过"。"""
    real = db_project.device.modes
    monkeypatch.setattr(
        db_project.device, "modes",
        lambda ip: None if ip == "USART1" else real(ip),
    )
    issues, skipped = validate.check_modes(db_project)

    assert skipped is None
    found = [i for i in issues if i.code == "modes.unavailable"]
    assert found and found[0].level == "info"


# ==================================================== 第 4 层：参数合法性


def test_param_written_without_registration(db_project: IocProject):
    """写了 ``USART1.BaudRate`` 却没进 ``IPParameters`` —— CubeMX 会丢掉这一行。"""
    db_project.doc.set("USART1.BaudRate", "115200")

    found = issue(validate.validate(db_project), "param.unregistered")
    assert found.level == "error"
    assert "IPParameters" in found.message
    assert "--param BaudRate=115200" in found.hint


def test_registered_param_without_a_value(db_project: IocProject):
    db_project.doc.set("USART1.IPParameters", "VirtualMode,BaudRate")

    found = issue(validate.validate(db_project), "param.dangling")
    assert found.level == "warning"
    assert "BaudRate" in found.message


def test_param_that_does_not_belong_to_the_mode(db_project: IocProject):
    db_project.find_peripheral("USART1").set_param("ClockPolarity", "HIGH")

    found = issue(validate.validate(db_project), "param.not_in_mode")
    assert found.subject == "USART1"
    assert "Asynchronous" in found.message


def test_param_value_out_of_range(db_project: IocProject):
    db_project.find_peripheral("USART1").set_param("Parity", "NONSENSE")

    found = issue(validate.validate(db_project), "param.bad_value")
    assert "PARITY_NONE" in found.hint


def test_virtual_mode_is_not_double_reported_as_a_param(db_project: IocProject):
    """``VirtualMode`` 由第 3 层管，第 4 层不能再说它"不属于这个模式"。"""
    assert "param.not_in_mode" not in codes(validate.validate(db_project))


def test_system_peripheral_params_are_skipped(db_project: IocProject):
    """``NVIC.*`` / ``RCC.*`` 走自己的命名空间，不用 IPParameters 登记。"""
    report = validate.validate(db_project)
    assert not [i for i in report.issues if i.subject in ("NVIC", "RCC", "DMA")]


# ============================================ GPIO 参数登记表（同一套机制）


def test_gpio_param_written_without_registration(db_project: IocProject):
    """``PA9.GPIO_PuPd`` 没登记进 ``GPIOParameters``，CubeMX 加载时会丢掉。"""
    db_project.doc.set("PA9.GPIO_PuPd", "GPIO_NOPULL")

    found = issue(validate.validate(db_project), "gpio.param_unregistered")
    assert found.level == "error"
    assert "GPIOParameters" in found.message


def test_gpio_registry_entry_without_a_value(db_project: IocProject):
    db_project.doc.set("PA9.GPIOParameters", "GPIO_PuPd")

    found = issue(validate.validate(db_project), "gpio.param_dangling")
    assert found.level == "warning"
    assert "GPIO_PuPd" in found.message


def test_structural_pin_keys_are_not_demanded_in_the_registry(db_project: IocProject):
    """``Signal`` / ``Mode`` / ``Locked`` 不进登记表 —— 别误报成未登记。"""
    report = validate.validate(db_project)
    assert "gpio.param_unregistered" not in codes(report)
    assert "gpio.param_dangling" not in codes(report)


def test_label_written_by_the_tool_is_registered_and_clean(db_project: IocProject):
    ops.add_gpio(db_project, "PB0", mode="Output", label="LED")
    report = validate.validate(db_project)
    assert "gpio.param_unregistered" not in codes(report)
    assert report.errors == [], report.render()


def test_virtual_pins_skip_the_gpio_registry_check(db_project: IocProject):
    ops.add_peripheral(db_project, "TIM3")
    assert "gpio.param_unregistered" not in codes(validate.validate(db_project))


# ============================================================ 重复 signal


def test_the_same_signal_on_two_pins(db_project: IocProject):
    db_project.doc.set("PA10.Signal", "USART1_TX")

    found = issue(validate.validate(db_project), "signal.duplicated")
    assert found.level == "error"
    assert "PA9" in found.message and "PA10" in found.message


def test_gpio_signals_may_legitimately_repeat(db_project: IocProject):
    """几十个脚同时是 ``GPIO_Output`` 是正常的，报出来只会淹没真正的问题。"""
    for pin in ("PB0", "PB1", "PC0", "PC1", "PD0"):
        ops.add_gpio(db_project, pin, mode="Output")

    report = validate.validate(db_project)
    assert "signal.duplicated" not in codes(report)
    assert report.errors == [], report.render()


def test_exti_signals_are_per_pin_and_never_duplicate(db_project: IocProject):
    ops.add_gpio(db_project, "PB0", mode="EXTI")
    ops.add_gpio(db_project, "PB1", mode="EXTI")

    report = validate.validate(db_project)
    assert db_project.doc.get("PB0.Signal") == "GPXTI0"
    assert db_project.doc.get("PB1.Signal") == "GPXTI1"
    assert "signal.duplicated" not in codes(report)


# ============================================================ 报告与降级


def test_warnings_do_not_block(db_project: IocProject):
    db_project.add_peripheral("SPI2")  # ip.no_pins，warning

    report = validate.validate(db_project)
    assert report.warnings and not report.errors
    assert report.ok  # 只有 error 才挡落盘


def test_errors_block(db_project: IocProject):
    db_project.doc.set("PA9.Mode", "Nonsense")
    report = validate.validate(db_project)
    assert report.errors and not report.ok


def test_report_says_what_was_skipped_when_the_db_is_missing(project: IocProject):
    """没有数据库时第 1/3/4 层要让位并明说，不能谎报"通过"。"""
    report = validate.validate(project)

    assert len(report.skipped) >= 2
    assert any("引脚能力" in s for s in report.skipped)
    assert any("模式" in s for s in report.skipped)
    assert "跳过" in report.render()
    # 但需要数据库才能发现的错，此时一条都不该冒出来
    assert report.errors == []


def test_registration_checks_still_run_without_the_db(project: IocProject):
    """第 2 层不需要数据库，不该跟着一起被跳过。"""
    project.doc.set("PA9.Signal", "USART2_TX")
    assert "ip.not_registered" in codes(validate.validate(project))


def test_validate_can_take_the_device_as_an_argument(project: IocProject, device):
    report = validate.validate(project, device=device)
    assert project.device is device
    assert report.skipped == []


def test_report_renders_every_issue_with_code_subject_and_hint(db_project: IocProject):
    db_project.doc.set("PA9.Mode", "Nonsense")
    text = validate.validate(db_project).render()

    # code 要在纯文本里也能看见 —— 脚本/AI 才好按类别处理
    assert "[error] mode.unknown PA9:" in text
    assert "建议：" in text
    assert "个错误" in text


def test_only_wrong_keys_block_not_unfinished_ones(db_project: IocProject):
    """``blocking_errors`` 把"配错了"和"还没配完"分开 —— CLI 的写闸门靠它。"""
    db_project.add_peripheral("SPI2")           # ip.no_pins：warning，不算
    assert validate.blocking_errors(validate.validate(db_project)) == []

    db_project.doc.set("PA10.Mode", "Nonsense")  # 模式名不存在：配错了
    found = {i.code for i in validate.blocking_errors(validate.validate(db_project))}
    # mode.unknown（这个模式名不存在）+ mode.inconsistent（和 PA9 打架了）
    assert found == {"mode.unknown", "mode.inconsistent"}


def test_an_unfinished_peripheral_is_an_error_but_not_blocking(db_project: IocProject):
    """``mode.missing_signals`` 仍是 error（总检查该报），但不挡逐字段编辑。"""
    db_project.doc.discard("PA10.Signal")
    report = validate.validate(db_project)

    assert "mode.missing_signals" in codes(report)
    assert not report.ok
    assert validate.blocking_errors(report) == []


# ==================================================== NVIC 的坏键与孤儿请求


def test_the_nvic_registry_key_is_flagged(db_project: IocProject):
    """``NVIC.IParameters`` 是别的键名空间的习惯写法，CubeMX 会静默丢掉它。

    精确点名这一个键是刻意的：``NVIC.*`` 的键名空间是私有的，做通用白名单一定会在
    没见过的 MCU 上误报。而这一条查得实：**524 份 ST 官方板级文件里出现 0 次**。
    """
    db_project.doc.set("NVIC.IParameters", "PriorityGroup")

    report = validate.validate(db_project)
    found = issue(report, "nvic.bogus_key")
    assert "静默" in found.message
    # 只是提醒：它还没坏，CubeMX 一保存才会坏
    assert validate.blocking_errors(report) == []
    assert report.ok


def test_a_normal_nvic_key_is_not_flagged(db_project: IocProject):
    """别把 ``NVIC.PriorityGroup`` / ``NVIC.ForceEnableDMAVector`` 一起误伤了。"""
    assert "NVIC.PriorityGroup" in db_project.doc
    assert "nvic.bogus_key" not in codes(validate.validate(db_project))


def _orphan(project: IocProject, ip: str) -> None:
    """把一个外设造成那种形态：登记进 ``Mcu.IP*``、声明了模式、
    **一个引脚都不占**。

    得手工造，因为 `ops.clear_pin` 收拾得很干净：它会把"已经没有引脚的孤儿外设"
    连同它的键一起摘掉，所以**走工具的路径永远到不了这个状态**。会落到这里的只有
    手改过的、或者旧版本留下的文件 —— 那正是规则要挡的东西。
    """
    project.add_peripheral(ip).set_virtual("VirtualMode", "VM_ASYNC")


def test_an_irq_on_a_peripheral_with_no_pins_is_flagged(db_project: IocProject):
    """实测：这种工程 CubeMX 一保存就把
    ``Mcu.IP*`` 里的外设**和 ``NVIC.<IRQn>`` 那一行**一起删掉，不报任何错。

    `validate` 是唯一能在**写盘之前**说这件事的地方 —— 万一没跑 `--verify`。
    """
    _orphan(db_project, "USART2")
    ops.enable_irq(db_project, "USART2_IRQn")

    found = issue(validate.validate(db_project), "irq.orphan")
    assert found.subject == "USART2_IRQn" and "USART2" in found.message
    # 只是提醒：现在还没坏，CubeMX 一保存才坏
    assert validate.blocking_errors(validate.validate(db_project)) == []


def test_a_dma_request_on_a_peripheral_with_no_pins_is_flagged(db_project: IocProject):
    """DMA 那一半更阴：CubeMX 会把 ``Dma.*`` 块**原样留着**，看起来完全正常。

    所以往返验证抓不到这一种（实测用例 C），只有这条静态规则能说。
    """
    _orphan(db_project, "USART2")
    ops.add_dma(db_project, "USART2_RX")

    found = issue(validate.validate(db_project), "dma.orphan")
    assert found.subject == "USART2_RX" and "USART2" in found.message


def test_orphan_checks_do_not_fire_on_a_configured_peripheral(db_project: IocProject):
    """样本工程是 CubeMX 自己写出来的干净工程 —— 这两条一条都不该冒出来。"""
    assert codes(validate.validate(db_project)) & {"irq.orphan", "dma.orphan"} == set()


def test_a_pinless_peripheral_without_a_mode_is_not_called_an_orphan(
    db_project: IocProject,
):
    """**没有 ``VirtualMode`` 的"没引脚"不是问题** —— 这条是拿 524 份官方文件量出来的。

    ADC 走内部通道（``Channel-0#ChannelRegularConversion``）、DFSDM 走滤波器参数、
    TIM 走强制输出，都是合法的无引脚配置。第一版规则没带这个门槛，扫出来 5 份命中
    （``DFSDM1`` / ``ADC1`` / ``TIM1``），**没有一份是真的**：DFSDM1 的引脚信号叫
    ``S_DATAIN5DFSDM1``（实例名在后缀），ADC1 干脆用通道参数而不是引脚。
    加上模式键这个门槛之后 **512/512 全部干净**，而真会出事的那个探针
    （``VirtualMode=VM_ASYNC`` + 没引脚）照样报。

    模式键在这里是**判据**而不只是巧合：是模式在要求引脚。
    """
    _orphan(db_project, "USART2")
    ops.enable_irq(db_project, "USART2_IRQn")
    assert "irq.orphan" in codes(validate.validate(db_project))  # 先确认前提成立

    db_project.doc.discard("USART2.VirtualMode")

    assert "irq.orphan" not in codes(validate.validate(db_project))


def test_a_controller_that_never_has_pins_is_not_an_orphan(db_project: IocProject):
    """``NVIC.DMA2_Stream2_IRQn`` 的归属外设是 ``DMA``，而 ``DMA`` 永远没有引脚 ——
    拿"没引脚"说它，每一份正常工程都会挨一记（``Mcu.IP*=DMA`` 是 CubeMX 自己的写法）。

    库里对 ``DMA`` **一个信号都没有**，这个门槛先把它挡在外面。
    """
    report = validate.validate(db_project)
    assert {"irq.orphan", "dma.orphan"}.isdisjoint(codes(report))
