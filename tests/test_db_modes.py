"""``cubemx_mcp.db.modes`` —— 模式库解析。

大部分用例用**手写的小 XML**，不依赖本机的 CubeMX 安装：解析规则是纯 XML 逻辑，
用真库测反而把"解析对不对"和"ST 的数据怎么写的"混在一起。只有最后一组拿真库
钉住"TIM3 的虚拟引脚该写 ``Internal``"这个实测结论。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cubemx_mcp.db import Device, ModeDb

#: 一个最小但结构完整的模式库：``Enable_Timer``（无 UserName）与 ``Internal``
#: （有 UserName）都声明 ``VS_ClockSourceINT`` —— 这正是真实 TIM 库里的形状。
SAMPLE_MODES = """\
<?xml version="1.0" encoding="UTF-8"?>
<Mcu>
  <IP Name="TIM3" Version="1.0">
    <RefParameter Name="Prescaler" DefaultValue="0" Type="integer"/>
    <RefParameter Name="VirtualMode" DefaultValue="VM_ASYNC">
      <PossibleValue Value="VM_ASYNC"/>
    </RefParameter>
    <RefMode Name="timBasic" Abstract="true">
      <Parameter Name="Prescaler"/>
    </RefMode>
    <RefMode Name="Enable_Timer" BaseMode="timBasic">
      <Parameter Name="VirtualMode"><PossibleValue Value="VM_ASYNC"/></Parameter>
    </RefMode>
    <RefMode Name="Internal" BaseMode="timBasic" UserName="Internal Clock"/>
    <Mode Name="Enable_Timer" UserName="">
      <SignalLogicalOp Name="AND"><Signal Name="VS_ClockSourceINT"/></SignalLogicalOp>
    </Mode>
    <Mode Name="Internal" UserName="Internal Clock">
      <SignalLogicalOp Name="AND"><Signal Name="VS_ClockSourceINT"/></SignalLogicalOp>
    </Mode>
    <Mode Name="External">
      <SignalLogicalOp Name="AND"><Signal Name="VS_ClockSourceEXT"/></SignalLogicalOp>
    </Mode>
  </IP>
</Mcu>
"""


@pytest.fixture
def mdb(tmp_path: Path) -> ModeDb:
    p = tmp_path / "TIM3-1.0_Modes.xml"
    p.write_text(SAMPLE_MODES, encoding="utf-8")
    return ModeDb.load(p)


# ============================================================ Mode / UserName


def test_user_name_is_parsed_from_the_mode_element(mdb: ModeDb):
    """``UserName`` 是区分"GUI 里真能选"和"内部伪模式"的唯一依据。"""
    assert mdb.get("Internal").user_name == "Internal Clock"


def test_a_mode_without_user_name_has_none(mdb: ModeDb):
    assert mdb.get("External").user_name is None


def test_an_empty_user_name_does_not_count(mdb: ModeDb):
    """``UserName=""`` 是没写，不是写了空名字 —— 否则内部伪模式会被当成可选项。"""
    assert mdb.get("Enable_Timer").user_name is None


def test_ref_mode_user_name_is_readable_too(mdb: ModeDb):
    assert mdb.get("Internal").params  # timBasic 继承来的
    assert "Prescaler" in mdb.get("Internal").params


# ============================================================ VS 信号索引


def test_vs_signals_indexes_every_declaring_mode_in_file_order(mdb: ModeDb):
    assert mdb.vs_signals["VS_ClockSourceINT"] == ("Enable_Timer", "Internal")
    assert mdb.vs_signals["VS_ClockSourceEXT"] == ("External",)


def test_non_vs_signals_are_not_indexed(mdb: ModeDb):
    """只有 ``VS_`` 开头的才对应虚拟引脚；普通信号走 ``required_signals``。"""
    assert all(k.startswith("VS_") for k in mdb.vs_signals)


# ======================================================== virtual_pin_mode


def test_virtual_pin_mode_prefers_the_gui_selectable_mode(mdb: ModeDb):
    """``Enable_Timer`` 在文件里排前面，但该选有 UserName 的 ``Internal``。"""
    assert mdb.virtual_pin_mode("VS_ClockSourceINT") == "Internal"


def test_virtual_pin_mode_falls_back_to_the_first_declarer(mdb: ModeDb):
    """唯一声明者没有 UserName 时也得给出一个名字，不能返回 None 让调用方漏写。"""
    assert mdb.virtual_pin_mode("VS_ClockSourceEXT") == "External"


def test_virtual_pin_mode_returns_none_for_an_unknown_signal(mdb: ModeDb):
    """库里查不到就不猜 —— 调用方回退到外设模式名（见 `ops.add_peripheral`）。"""
    assert mdb.virtual_pin_mode("VS_Nonsense") is None


# ============================================================ 真库实测结论


def test_the_real_tim3_library_says_internal(device: Device):
    """实测：TIM3 的默认模式是 ``Enable_Timer``，但虚拟引脚要写 ``Internal``。

    三变体 CubeMX 往返实验：漏写 ``Mode`` 时 TIM3 会从
    ``Mcu.IP*`` 里消失，写 ``Enable_Timer`` 会被改写，只有 ``Internal`` 原样保留。
    """
    mdb = device.modes("TIM3")
    assert mdb is not None
    assert mdb.default_mode().name == "Enable_Timer"
    assert mdb.virtual_pin_mode("VS_ClockSourceINT") == "Internal"


def test_the_real_sys_library_maps_each_timer_to_a_vs_signal(device: Device):
    mdb = device.modes("SYS")
    assert mdb is not None
    assert mdb.virtual_pin_mode("VS_Systick") == "SysTick"
    assert mdb.virtual_pin_mode("VS_tim3") == "TIM3"
