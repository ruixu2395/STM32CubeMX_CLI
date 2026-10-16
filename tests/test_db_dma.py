"""``cubemx_mcp.db.dma`` —— DMA 请求表。

这份文件里有两样别处没有的东西：**请求→外设**的关联（``IpInstance``，只以
每模式覆盖的形式存在），以及**请求→stream** 的映射（藏在 ``<ModeLogicOperator>``
树里）。两样都只能从这个文件读出来，所以都值得钉住。

参数表整个复用 `ModeDb`，这里只测"确实复用上了"，不重复测 ``<RefParameter>``。
"""

from __future__ import annotations

import pytest

from cubemx_mcp.db import Device, DmaDb
from cubemx_mcp.db.xml import DbError

#: 最小但结构完整的 DMA 表。树是**四层**（根 / DMA1 / stream / 请求），和真实的
#: F4 库同形；``DMA1_Stream4`` 故意留空，用来钉"空 stream 不该凭空产出请求"。
SAMPLE_DMA = """\
<?xml version="1.0" encoding="UTF-8"?>
<Mcu>
  <IP Name="DMA" Version="2.0">
    <RefParameter Name="Priority" DefaultValue="DMA_PRIORITY_LOW">
      <PossibleValue Value="DMA_PRIORITY_LOW"/>
      <PossibleValue Value="DMA_PRIORITY_MEDIUM"/>
    </RefParameter>
    <RefParameter Name="Mode" DefaultValue="DMA_NORMAL">
      <PossibleValue Value="DMA_NORMAL"/>
      <PossibleValue Value="DMA_CIRCULAR"/>
    </RefParameter>
    <RefMode Name="DMA_Request" Abstract="true">
      <Parameter Name="Instance"/>
      <Parameter Name="Priority"/>
      <Parameter Name="Mode"/>
    </RefMode>
    <RefMode Name="MEMTOMEM" BaseMode="DMA_Request"/>
    <RefMode Name="USART1_RX" BaseMode="DMA_Request">
      <Parameter Name="IpInstance"><PossibleValue Value="USART1"/></Parameter>
      <Parameter Name="Channel"><PossibleValue Value="DMA_CHANNEL_4"/></Parameter>
      <Parameter Name="Direction"><PossibleValue Value="DMA_PERIPH_TO_MEMORY"/></Parameter>
    </RefMode>
    <RefMode Name="USART1_TX" BaseMode="DMA_Request">
      <Parameter Name="IpInstance"><PossibleValue Value="USART1"/></Parameter>
      <Parameter Name="Direction"><PossibleValue Value="DMA_MEMORY_TO_PERIPH"/></Parameter>
    </RefMode>
    <RefMode Name="SPI1_RX" BaseMode="DMA_Request">
      <Parameter Name="IpInstance"><PossibleValue Value="SPI1"/></Parameter>
    </RefMode>
    <ModeLogicOperator Name="OR">
      <Mode Name="DMA1">
        <ModeLogicOperator Name="OR">
          <Mode Name="DMA1_Stream0">
            <ModeLogicOperator Name="XOR">
              <Mode Name="USART1_RX"/>
              <Mode Name="SPI1_RX"><Condition Expression="SPI1_DmaReceive"/></Mode>
            </ModeLogicOperator>
          </Mode>
          <Mode Name="DMA1_Stream2">
            <ModeLogicOperator Name="XOR">
              <Mode Name="USART1_RX"/>
            </ModeLogicOperator>
          </Mode>
          <Mode Name="DMA1_Stream4">
            <ModeLogicOperator Name="XOR"/>
          </Mode>
        </ModeLogicOperator>
      </Mode>
      <Mode Name="DMA2">
        <ModeLogicOperator Name="OR">
          <Mode Name="DMA2_Stream5">
            <ModeLogicOperator Name="XOR">
              <Mode Name="USART1_RX"/>
              <Mode Name="USART1_TX"/>
            </ModeLogicOperator>
          </Mode>
        </ModeLogicOperator>
      </Mode>
    </ModeLogicOperator>
  </IP>
</Mcu>
"""


@pytest.fixture
def ddb(tmp_path) -> DmaDb:
    p = tmp_path / "DMA-STM32FAKE_Modes.xml"
    p.write_text(SAMPLE_DMA, encoding="utf-8")
    return DmaDb.load(p)


# ========================================================= 请求 → 外设


def test_requests_are_found_by_their_ip_instance(ddb: DmaDb):
    """``IpInstance`` 只在每模式的覆盖里，顶层 ``<RefParameter>`` 里没有 ——
    所以 `ModeDb.param("IpInstance").values` 是空的，必须自己读。"""
    assert [r.name for r in ddb.requests_for("USART1")] == ["USART1_RX", "USART1_TX"]
    assert [r.name for r in ddb.requests_for("SPI1")] == ["SPI1_RX"]
    assert ddb.requests_for("USART2") == ()


def test_internal_modes_are_not_requests(ddb: DmaDb):
    """``MEMTOMEM`` 是内存到内存，不属于任何外设；``DMA_Request`` 是抽象基类。

    漏掉这层过滤，"USART1 的 DMA 请求"里会冒出一条没有归属的条目。
    """
    names = [r.name for r in ddb.all()]
    assert names == ["USART1_RX", "USART1_TX", "SPI1_RX"]


# ========================================================= 请求 → stream


def test_streams_come_from_the_logic_operator_tree(ddb: DmaDb):
    """**这条是本文件存在的理由。** 一个请求能挂哪些 stream 只在这个树里。

    走的是递归而不是 `descendants()` —— 后者把父子关系拍平，拿到的 stream 和
    请求就分不清谁套着谁了。
    """
    assert ddb.request("USART1_RX").streams == ("DMA1_Stream0", "DMA1_Stream2", "DMA2_Stream5")
    assert ddb.request("USART1_TX").streams == ("DMA2_Stream5",)
    assert ddb.request("SPI1_RX").streams == ("DMA1_Stream0",)


def test_a_stream_with_no_requests_contributes_nothing(ddb: DmaDb):
    """``DMA1_Stream4`` 下面挂的是空的 XOR —— 不该产出任何映射。"""
    for r in ddb.all():
        assert "DMA1_Stream4" not in r.streams


def test_streams_are_deduplicated(ddb: DmaDb):
    """同一个 stream 在树里被引用两次也只算一次。"""
    assert len(set(ddb.request("USART1_RX").streams)) == 3


def test_the_tree_is_found_whatever_wraps_it(tmp_path):
    """**这条是回归。** 真实文件根元素就是 ``<IP>``（没有 ``<Mcu>`` 外壳），
    但上面那份样本按 ``<Mcu><IP>`` 包了一层 —— 两种都必须能读到树。

    早先的实现直接 ``children(root, "ModeLogicOperator")``，包一层就一个 stream
    都读不出来，而且是**静默**读不出来：看起来就像"这个请求挂不了任何 stream"。
    """
    stripped = SAMPLE_DMA.replace("<Mcu>\n", "").replace("</Mcu>\n", "")
    assert "<Mcu>" not in stripped  # 确保真的剥掉了，不然这条测试是假的
    p = tmp_path / "DMA-NoWrapper_Modes.xml"
    p.write_text(stripped, encoding="utf-8")

    assert DmaDb.load(p).request("USART1_RX").streams == (
        "DMA1_Stream0",
        "DMA1_Stream2",
        "DMA2_Stream5",
    )


# ============================================================ 参数（复用）


def test_params_are_inherited_through_base_mode(ddb: DmaDb):
    """参数表整个复用 `ModeDb` —— 这里只确认继承真的生效了。"""
    r = ddb.request("USART1_RX")
    assert "Priority" in r.params and "Mode" in r.params


def test_fixed_values_are_the_ones_with_a_single_possible_value(ddb: DmaDb):
    """硬件定死的参数（``Channel``/``Direction``）不给用户改，标成"固定"。"""
    r = ddb.request("USART1_RX")
    assert r.fixed_map == {
        "Channel": "DMA_CHANNEL_4",
        "Direction": "DMA_PERIPH_TO_MEMORY",
    }


def test_settable_hides_the_derived_and_fixed_params(ddb: DmaDb):
    """``Instance`` 是 stream 本身、``IpInstance`` 是归属标记、``Channel``/
    ``Direction`` 是固定值 —— 都不该出现在"还能改"的清单里。"""
    assert ddb.request("USART1_RX").settable == ("Mode", "Priority")


def test_param_order_is_stable(ddb: DmaDb):
    """**这条是回归。** ``mode_params`` 给的是 frozenset，直接 ``tuple()`` 出来的
    顺序随哈希种子变 —— 同一个工程两次运行打印的顺序会不一样，测试也会随机红。
    """
    assert ddb.request("USART1_RX").params == tuple(sorted(ddb.request("USART1_RX").params))


def test_param_and_default_value_delegate_to_the_mode_library(ddb: DmaDb):
    """调用方拿取值范围/默认值时不该知道参数表是从哪来的。"""
    assert ddb.default_value("Priority") == "DMA_PRIORITY_LOW"
    assert "DMA_PRIORITY_MEDIUM" in ddb.param("Priority").values


# ================================================================== 出错


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(DbError):
        DmaDb.load(tmp_path / "nope.xml")


def test_a_file_without_any_request_is_an_error(tmp_path):
    """格式变了要炸 —— 静默返回空表会让"这页看不了"变成"这外设没有 DMA"。"""
    p = tmp_path / "DMA-X_Modes.xml"
    p.write_text(
        '<?xml version="1.0"?><Mcu><IP Name="DMA"><RefParameter Name="X"/></IP></Mcu>',
        encoding="utf-8",
    )
    with pytest.raises(DbError):
        DmaDb.load(p)


# ============================================================ 真库实测结论


def test_the_real_f417_usart1_requests_match_the_hardware(device: Device):
    """实测 STM32F417：USART1 恰好两个请求，stream 与手册一致。

    手册里 USART1_RX 是 DMA2 的 Stream2/Stream5，USART1_TX 是 Stream7 ——
    这条断言对着**数据手册**而不是对着代码，所以它坏掉时是真的错了。
    """
    ddb = device.dma()
    assert ddb is not None

    rx = ddb.request("USART1_RX")
    assert ("DMA2_Stream2" in rx.streams) and ("DMA2_Stream5" in rx.streams)
    assert rx.streams == tuple(s for s in rx.streams if s.startswith("DMA2_Stream"))

    assert [r.name for r in ddb.requests_for("USART1")] == ["USART1_RX", "USART1_TX"]
    assert ddb.request("USART1_TX").streams == ("DMA2_Stream7",)


def test_the_real_f417_spi1_requests_are_on_dma2(device: Device):
    ddb = device.dma()
    assert ddb is not None
    assert "DMA2_Stream0" in ddb.request("SPI1_RX").streams
    assert "DMA2_Stream3" in ddb.request("SPI1_TX").streams
