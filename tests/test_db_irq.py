"""``cubemx_mcp.db.irq`` —— NVIC 中断表。

和 `test_db_modes.py` 一样分两段：解析规则用手写的小 XML 钉住，最后一段拿
真库确认"ST 的数据确实长这样"。分开是因为这两件事会**各自**坏掉 —— 真库换了
格式不该让解析规则的用例跟着红，而解析规则对了也不代表 ST 那份文件的结构没变。
"""

from __future__ import annotations

import pytest

from cubemx_mcp.db import Device, IrqDb
from cubemx_mcp.db.xml import DbError

#: 一份最小但结构完整的 NVIC 表。``IRQn`` 的隐藏参数、逗号归属、缺段的非法值、
#: 以及按分组分段（其中一段带 ``S_FREERTOS``）的优先级范围，全都是真实形状。
SAMPLE_NVIC = """\
<?xml version="1.0" encoding="UTF-8"?>
<Mcu>
  <IP Name="NVIC" Version="1.0">
    <RefParameter Name="PriorityGroup" DefaultValue="NVIC_PRIORITYGROUP_4">
      <PossibleValue Value="NVIC_PRIORITYGROUP_0"/>
      <PossibleValue Value="NVIC_PRIORITYGROUP_4"/>
    </RefParameter>
    <RefParameter Name="IRQn" Type="list" Visible="false">
      <PossibleValue Comment="USART1 global interrupt" Value="USART1_IRQn:Y:USART1::"/>
      <PossibleValue Value="TIM1_UP_TIM10_IRQn:Y:TIM1,TIM10::"/>
      <PossibleValue Comment="EXTI line 9..5" Value="EXTI9_5_IRQn:Y:EXTI5,EXTI6,EXTI9::"/>
      <PossibleValue Comment="DMA2 stream2 global interrupt" Value="DMA2_Stream2_IRQn:Y:DMA2::"/>
      <PossibleValue Comment="TAMP" Value="TAMP_STAMP_IRQn:Y:RTC::" Condition="RTC_TAMPER_INTERRUPT"/>
      <PossibleValue Value="NotEnoughFields"/>
      <PossibleValue Value=":Y:GHOST::"/>
    </RefParameter>
    <RefParameter Name="PreemptionPriority" Min="1" Max="7">
      <Condition Expression="NVIC_PRIORITYGROUP_4|S_FREERTOS"/>
    </RefParameter>
    <RefParameter Name="SubPriority" Min="8" Max="15">
      <Condition Expression="NVIC_PRIORITYGROUP_4|S_FREERTOS"/>
    </RefParameter>
    <RefParameter Name="PreemptionPriority" Min="0" Max="15">
      <Condition Expression="NVIC_PRIORITYGROUP_4"/>
    </RefParameter>
    <RefParameter Name="SubPriority" Min="0" Max="0">
      <Condition Expression="NVIC_PRIORITYGROUP_4"/>
    </RefParameter>
    <RefParameter Name="PreemptionPriority" Min="0" Max="0">
      <Condition Expression="NVIC_PRIORITYGROUP_0"/>
    </RefParameter>
    <RefParameter Name="SubPriority" Min="0" Max="15">
      <Condition Expression="NVIC_PRIORITYGROUP_0"/>
    </RefParameter>
  </IP>
</Mcu>
"""


@pytest.fixture
def idb(tmp_path) -> IrqDb:
    p = tmp_path / "NVIC-STM32FAKE_Modes.xml"
    p.write_text(SAMPLE_NVIC, encoding="utf-8")
    return IrqDb.load(p)


# ================================================================== 解析


def test_irq_tuple_fields_are_split_apart(idb: IrqDb):
    """``名字:标志:归属:HAL 函数:附加`` —— 五段各归各的，别整串当名字。"""
    irq = idb.get("USART1_IRQn")
    assert irq is not None
    assert irq.flags == "Y"
    assert irq.owners == ("USART1",)
    assert irq.handler == ""
    assert irq.comment == "USART1 global interrupt"


def test_comma_separated_owners_are_split(idb: IrqDb):
    """**这条是回归。** 共享中断向量 ``TIM1_UP_TIM10_IRQn`` 归 TIM1 也归 TIM10。

    不 split 的话 TIM1 查不到自己的中断，而且是**静默**查不到 —— 用户看到的是
    "TIM1 没有中断可开"，不是报错。
    """
    assert idb.get("TIM1_UP_TIM10_IRQn").owners == ("TIM1", "TIM10")
    assert [i.name for i in idb.for_instance("TIM1")] == ["TIM1_UP_TIM10_IRQn"]
    assert [i.name for i in idb.for_instance("TIM10")] == ["TIM1_UP_TIM10_IRQn"]


def test_a_shared_vector_is_found_from_each_owner(idb: IrqDb):
    """EXTI9_5 那一串：三个外设都该看到它，别只认第一个。"""
    for name in ("EXTI5", "EXTI6", "EXTI9"):
        assert [i.name for i in idb.for_instance(name)] == ["EXTI9_5_IRQn"]


def test_values_that_are_not_irq_tuples_are_dropped(idb: IrqDb):
    """段数不够的、名字为空的 —— 丢掉，**不能**让它们变成一条名字很怪的中断。"""
    assert idb.get("NotEnoughFields") is None
    assert idb.get("") is None
    assert "" not in idb.irqs


def test_condition_is_carried_through_not_evaluated(idb: IrqDb):
    """条件表达式是 CubeMX 私有语法，只带出来给人看，不求值。"""
    assert idb.get("TAMP_STAMP_IRQn").condition == "RTC_TAMPER_INTERRUPT"
    assert idb.get("USART1_IRQn").condition is None


def test_for_stream_looks_the_name_up_instead_of_guessing(idb: IrqDb):
    """``DMA2_Stream2`` -> ``DMA2_Stream2_IRQn`` 走查表。

    表里没有就返回 None（而不是拼一个名字出来）—— 拼错了会静默变成
    "这个 stream 没有中断"，查不到至少调用方知道是"没有"。
    """
    assert idb.for_stream("DMA2_Stream2").name == "DMA2_Stream2_IRQn"
    assert idb.for_stream("DMA2_Stream9") is None
    assert idb.for_stream("USART2") is None


def test_for_instance_returns_empty_for_an_unknown_peripheral(idb: IrqDb):
    assert idb.for_instance("NOSUCH") == ()


# ============================================================== 优先级范围


def test_priority_range_reads_the_range_for_the_group(idb: IrqDb):
    assert idb.priority_range("NVIC_PRIORITYGROUP_4") == (0, 15, 0, 0)
    assert idb.priority_range("NVIC_PRIORITYGROUP_0") == (0, 0, 0, 15)


def test_freertos_guarded_range_does_not_win(idb: IrqDb):
    """同一个分组有两条声明，一条只在用了 FreeRTOS 时成立。

    取错那条会把抢占优先级报成 1-7 —— 用户照着填 8 会被判越界，而他没做错什么。
    """
    assert idb.priority_range("NVIC_PRIORITYGROUP_4")[1] == 15


def test_priority_group_default_is_read(idb: IrqDb):
    assert idb.priority_group == "NVIC_PRIORITYGROUP_4"
    # 不给分组时按 DefaultValue 走
    assert idb.priority_range() == (0, 15, 0, 0)


def test_unknown_group_falls_back_to_the_wide_range(idb: IrqDb):
    """**宁可放过也不误杀**：认不出分组就给全宽范围，不猜一个具体的。"""
    assert idb.priority_range("NVIC_PRIORITYGROUP_NONSENSE") == (0, 15, 0, 15)


# ================================================================== 出错


def test_missing_file_is_an_error_not_an_empty_table(tmp_path):
    """文件不在要报错。返回空表会让"这页看不了"变成"这颗 MCU 没有中断"。"""
    with pytest.raises(DbError):
        IrqDb.load(tmp_path / "nope.xml")


def test_a_file_without_the_irq_parameter_is_an_error(tmp_path):
    """**格式变了要炸**，不能静默返回空 —— 这条是 `ModeDb` 那个 guard 的翻版。"""
    p = tmp_path / "NVIC-X_Modes.xml"
    p.write_text(
        '<?xml version="1.0"?><Mcu><IP Name="NVIC"><RefParameter Name="Other"/></IP></Mcu>',
        encoding="utf-8",
    )
    with pytest.raises(DbError):
        IrqDb.load(p)


# ============================================================ 真库实测结论


def test_the_real_f417_table_has_the_shared_vectors(device: Device):
    """实测 STM32F417：91 条中断，且共享向量确实按逗号归属。"""
    idb = device.irqs()
    assert idb is not None
    assert len(idb.all()) == 91

    tim = idb.get("TIM1_UP_TIM10_IRQn")
    assert tim is not None and "TIM1" in tim.owners and "TIM10" in tim.owners
    assert "DMA2_Stream2_IRQn" in idb.irqs


def test_the_real_f417_priority_group_is_group4(device: Device):
    idb = device.irqs()
    assert idb is not None
    assert idb.priority_group == "NVIC_PRIORITYGROUP_4"
    assert idb.priority_range("NVIC_PRIORITYGROUP_4") == (0, 15, 0, 0)
    assert idb.priority_range("NVIC_PRIORITYGROUP_0") == (0, 0, 0, 15)


def test_the_real_nvic_file_cannot_be_loaded_as_a_mode_library(device: Device):
    """**这是 `db/irq.py` 存在的理由**：NVIC 那个文件里一个 ``<RefMode>`` 都没有。

    这条同时钉住"别去放宽 `db/modes.py` 那条 guard"—— 放宽之后
    `ops.list_modes("NVIC")` 会从一句清楚的报错变成一个静默的空列表。
    """
    assert device.modes("NVIC") is None
