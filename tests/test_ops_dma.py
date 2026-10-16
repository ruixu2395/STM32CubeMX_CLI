"""`ops.add_dma` / `ops.remove_dma`。

`remove_dma` 是唯一会**毁数据**的方向，而且索引是耦合的：``Dma.Request<n>`` 和
``Dma.<请求>.<n>.*`` 里的 ``n`` 是同一个全局序号，删掉中间一个，后面每个请求的名字键
和槽位键都要整体前移一位。所以这里的断言盯着**整个键集合**，不是盯着 `RequestsNb` ——
半截改名的文件也能让 `RequestsNb` 看起来是对的。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from cubemx_mcp import ops
from cubemx_mcp.ioc.project import IocProject


def _dma_keys(project: IocProject) -> list[str]:
    return sorted(k for k in project.doc.entries if k.startswith("Dma."))


def _three_requests(project: IocProject) -> IocProject:
    """样本工程（两个请求）+ 第三个 —— 删中间那个才测得出重排。"""
    ops.add_dma(project, "USART2_RX")
    assert project.doc.get("Dma.Request2") == "USART2_RX"
    return project


# ------------------------------------------------------------------ add_dma


def test_add_dma_picks_a_free_stream_and_says_which(db_project: IocProject) -> None:
    """没给 ``--stream`` 就自动挑，**并且把挑中的那个打进改动清单**。

    不打印的话用户根本不知道它挂到哪儿去了，而那是这份配置里最要紧的一项。
    """
    changes = ops.add_dma(db_project, "USART2_RX")

    picked = db_project.doc.get("Dma.USART2_RX.2.Instance")
    assert picked is not None
    assert any(picked in c for c in changes), changes


def test_add_dma_does_not_reuse_a_taken_stream(db_project: IocProject) -> None:
    taken = {p[1] for p in ops._dma_state(db_project).values()}
    ops.add_dma(db_project, "USART2_RX")
    assert db_project.doc.get("Dma.USART2_RX.2.Instance") not in taken


def test_add_dma_with_an_illegal_stream_lists_every_option(
    db_project: IocProject,
) -> None:
    """报错把**全部**合法 stream 列出来 —— 这就是"我该用哪个"的最快答法。"""
    with pytest.raises(ops.OpError) as exc:
        ops.add_dma(db_project, "USART2_RX", stream="DMA1_Stream0")
    message = str(exc.value)
    assert "不能挂在" in message
    assert "可选：" in message


def test_add_dma_keeps_the_count_in_step_with_the_requests(
    db_project: IocProject,
) -> None:
    _three_requests(db_project)
    assert db_project.doc.get("Dma.RequestsNb") == "3"
    assert _dma_keys(db_project).count("Dma.Request2") == 1


def test_add_dma_registers_the_dma_controller(db_project: IocProject) -> None:
    """DMA 要登记进 ``Mcu.IP*`` —— 实测 17/17 份有 Dma.* 的官方板级文件都登记了。"""
    ops.remove_dma(db_project, "USART1_RX", irq=False)
    ops.remove_dma(db_project, "USART1_TX", irq=False)
    assert db_project.find_peripheral("DMA") is None  # 删空时已退场

    changes = ops.add_dma(db_project, "USART1_RX")
    assert db_project.find_peripheral("DMA") is not None
    assert any("DMA" in c for c in changes)


def test_add_dma_copies_the_parameter_list_already_in_the_project(
    db_project: IocProject,
) -> None:
    """参数清单照工程里已有的那份抄，**不是照库里的**。

    库里 `DmaRequest.params` 按 `BaseMode` 展开，比 CubeMX 实际写进文件的多 ——
    照它写会多出 ``Channel`` / ``DMA_Handle`` / ``MemBurst`` 这些 CubeMX 从来不写的键。
    """
    template = (db_project.doc.get("Dma.USART1_RX.0.RequestParameters") or "").split(",")

    ops.add_dma(db_project, "USART2_RX")

    got = (db_project.doc.get("Dma.USART2_RX.2.RequestParameters") or "").split(",")
    # 集合一致（顺序照抄模板）；用户没点名要别的东西
    assert set(got) == set(template)
    assert "DMA_Handle" not in got and "IpInstance" not in got


def test_add_dma_turns_on_the_stream_irq(db_project: IocProject) -> None:
    ops.add_dma(db_project, "USART2_RX")
    irq_key = ops._stream_irq_key(db_project, db_project.doc.get("Dma.USART2_RX.2.Instance"))
    assert irq_key is not None and irq_key in db_project.doc


def test_add_dma_can_skip_the_irq(db_project: IocProject) -> None:
    ops.add_dma(db_project, "USART2_RX", irq=False)
    stream = db_project.doc.get("Dma.USART2_RX.2.Instance")
    assert ops._stream_irq_key(db_project, stream) is None


def test_add_dma_refuses_a_request_that_is_already_there(
    db_project: IocProject,
) -> None:
    """已经挂着的是**拒绝**，不是"没有改动" —— 用户可能正想换 stream。"""
    with pytest.raises(ops.OpError) as exc:
        ops.add_dma(db_project, "USART1_RX")
    assert "已经挂在" in str(exc.value)


def test_add_dma_rejects_an_unknown_request_with_the_owners_requests(
    db_project: IocProject,
) -> None:
    with pytest.raises(ops.OpError) as exc:
        ops.add_dma(db_project, "USART1_XX")
    assert "USART1_RX" in str(exc.value)


# ------------------------------------------------------------------ remove_dma


def test_removing_the_middle_request_renumbers_everything_after_it(
    db_project: IocProject,
) -> None:
    """删中间那个：后面的名字键和槽位键**都**要前移一位，且不能互相覆盖。

    盯着整个 `Dma.*` 键集合断言 —— 半截改名的文件也能让 `RequestsNb` 看起来正常。
    """
    _three_requests(db_project)
    rx_before = {k for k in _dma_keys(db_project) if k.startswith("Dma.USART1_RX.")}
    props = {
        k.rsplit(".", 1)[1] for k in _dma_keys(db_project) if k.startswith("Dma.USART2_RX.2.")
    }

    ops.remove_dma(db_project, "USART1_TX", irq=False)

    # 1) 前面的请求一个键都没被动过
    assert {k for k in _dma_keys(db_project) if k.startswith("Dma.USART1_RX.")} == rx_before
    # 2) 后面的请求整体落到 1 号槽，参数一个不少
    assert {
        k.rsplit(".", 1)[1] for k in _dma_keys(db_project) if k.startswith("Dma.USART2_RX.1.")
    } == props
    # 3) 待删的那个请求、以及空出来的 2 号槽，一个键都不剩
    assert not [k for k in _dma_keys(db_project) if "USART1_TX" in k or ".2." in k]
    assert db_project.doc.get("Dma.Request0") == "USART1_RX"
    assert db_project.doc.get("Dma.Request1") == "USART2_RX"
    assert db_project.doc.get("Dma.RequestsNb") == "2"


def test_removing_the_first_request_keeps_the_other_stream_assignment(
    db_project: IocProject,
) -> None:
    """最常见的删除：删第一个，第二个整体落到 0 号槽，**stream 不能串**。"""
    ops.remove_dma(db_project, "USART1_RX", irq=False)

    assert db_project.doc.get("Dma.Request0") == "USART1_TX"
    assert db_project.doc.get("Dma.USART1_TX.0.Instance") == "DMA2_Stream7"
    assert db_project.doc.get("Dma.RequestsNb") == "1"
    assert not [k for k in _dma_keys(db_project) if "USART1_RX" in k]


def test_renumbering_moves_values_raw_without_double_escaping(
    db_project: IocProject,
) -> None:
    """被搬动的值必须**原样**搬 —— ``get``/``set`` 兜一圈会把 ``\\:`` 变成 ``\\\\:``。

    那是一条真正的静默损坏：文件还是能读，值已经不是原来那个了。
    """
    _three_requests(db_project)
    # 造一个带转义冒号的值放在**会被搬动**的那一格上
    db_project.doc.set_raw("Dma.USART2_RX.2.Foo", "a\\:b")
    assert db_project.doc.get_raw("Dma.USART2_RX.2.Foo") == "a\\:b"

    ops.remove_dma(db_project, "USART1_TX", irq=False)

    assert db_project.doc.get_raw("Dma.USART2_RX.1.Foo") == "a\\:b"


def test_removing_the_last_request_takes_the_whole_block_with_it(
    db_project: IocProject,
) -> None:
    """一个请求都不剩时：**删掉 RequestsNb，不是写 0**。

    实测 524 份 ST 官方板级文件里 `RequestsNb` 出现过 17 次（1/2/3/6/7/9），
    **0 次都没出现过** —— "没有 DMA"在这份文件里的真身是"整块都不存在"。
    """
    changes = []

    ops.remove_dma(db_project, "USART1_RX")
    changes += ops.remove_dma(db_project, "USART1_TX")

    assert _dma_keys(db_project) == []
    assert "Dma.RequestsNb" not in db_project.doc
    assert db_project.find_peripheral("DMA") is None
    assert any("RequestsNb" in c for c in changes)


def test_removing_the_last_request_leaves_force_enable_dma_vector_alone(
    db_project: IocProject,
) -> None:
    """``NVIC.ForceEnableDMAVector`` 是**全局 NVIC 设置，不是 DMA 的标记**。

    实测 205 份文件带它，其中 193 份一个 `Dma.*` 键都没有 —— 顺手删掉它在多数
    情况下就是错的。
    """
    assert "NVIC.ForceEnableDMAVector" in db_project.doc
    ops.remove_dma(db_project, "USART1_RX")
    ops.remove_dma(db_project, "USART1_TX")
    assert "NVIC.ForceEnableDMAVector" in db_project.doc


def test_removing_a_request_disables_its_stream_irq(db_project: IocProject) -> None:
    """和 add_dma 的默认开对称：加的时候自动开，删的时候自动关，两个方向都打出来。"""
    assert "NVIC.DMA2_Stream2_IRQn" in db_project.doc

    changes = ops.remove_dma(db_project, "USART1_RX")

    assert "NVIC.DMA2_Stream2_IRQn" not in db_project.doc
    assert any("DMA2_Stream2_IRQn" in c for c in changes)
    # 另一个 stream 的中断不能跟着遭殃
    assert "NVIC.DMA2_Stream7_IRQn" in db_project.doc


def test_removing_a_request_can_keep_the_irq(db_project: IocProject) -> None:
    ops.remove_dma(db_project, "USART1_RX", irq=False)
    assert "NVIC.DMA2_Stream2_IRQn" in db_project.doc


def test_removing_an_unknown_request_lists_what_is_there(
    db_project: IocProject,
) -> None:
    with pytest.raises(ops.OpError) as exc:
        ops.remove_dma(db_project, "USART9_RX")
    assert "USART1_RX" in str(exc.value)


def test_removing_something_that_is_not_a_request_at_all_is_refused(
    db_project: IocProject,
) -> None:
    with pytest.raises(ops.OpError):
        ops.remove_dma(db_project, "USART1")


def test_a_removed_then_re_added_request_lands_in_the_same_place(
    db_project: IocProject,
) -> None:
    """删了再加回来应该回到同一个 stream —— 否则"删了重配"就变成了偷偷换硬件通道。"""
    stream = db_project.doc.get("Dma.USART1_RX.0.Instance")

    ops.remove_dma(db_project, "USART1_RX", irq=False)
    ops.add_dma(db_project, "USART1_RX", irq=False)

    assert db_project.doc.get("Dma.USART1_RX.1.Instance") == stream


def test_dma_ops_need_a_device(project: IocProject) -> None:
    with pytest.raises(ops.OpError) as exc:
        ops.add_dma(project, "USART1_RX")
    assert "CubeMX 数据库" in str(exc.value)


def test_a_request_with_no_candidate_channels_is_not_called_a_conflict(
    db_project: IocProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库里一个候选通道都没有时，**不能说成"都被占用了"** —— 那是件没核对过的事。

    实测 STM32WB5MMGHx（带 DMAMUX）：``Dma.I2C1_RX.0.Instance=DMA1_Channel1`` 好好地
    写在文件里，而库里对 I2C1_RX 的 ``streams`` 是空的 —— XML 里连一个
    ``DMA*_Channel*`` / ``DMA*_Stream*`` 形态的 Mode 都不存在，因为这类 MCU 的
    请求→通道映射由 DMAMUX 随时指定，不在 ST 的数据库里。报成占用冲突，
    用户会去查一个根本不存在的冲突。
    """
    dma_db = db_project.device.dma()
    real = dma_db.request("USART2_RX")
    assert real is not None and real.streams  # 先确认样本工程本来是有候选的

    stripped = dataclasses.replace(real, streams=())
    lookup = type(dma_db).request
    monkeypatch.setattr(
        type(dma_db),
        "request",
        lambda self, name: stripped if name == "USART2_RX" else lookup(self, name),
    )

    with pytest.raises(ops.OpError) as exc:
        ops.add_dma(db_project, "USART2_RX")

    message = str(exc.value)
    assert "没有可自动挑选的通道" in message
    assert "占用" not in message
    assert "--stream" in message


def test_add_dma_rejects_a_stream_that_is_not_in_the_fixture(
    db_project: IocProject,
) -> None:
    """非法参数名要在**写任何键之前**就被拒 —— 别留下一半的改动。"""
    before = _dma_keys(db_project)
    with pytest.raises(ops.OpError):
        ops.add_dma(db_project, "USART2_RX", params={"NoSuchThing": "x"})
    assert _dma_keys(db_project) == before


def test_removing_a_request_does_not_touch_unrelated_pins(
    db_project: IocProject,
) -> None:
    """DMA 块和引脚没关系 —— 删它不该顺手动了 PA9/PA10。"""
    before = db_project.render()
    ops.remove_dma(db_project, "USART1_RX", irq=False)
    after = db_project.render()
    assert "PA9.Signal=USART1_TX" in after and "PA10.Signal=USART1_RX" in after
    # 唯一该变的就是 Dma.* 那几行
    assert sum(1 for a, b in zip(before.splitlines(), after.splitlines()) if a != b) > 0


def test_no_cubemx_round_trip_needed_for_a_pure_dma_edit(
    db_project: IocProject, tmp_path: Path
) -> None:
    """改完能原样存回并重新读出来 —— 键名/转义都没写坏。"""
    _three_requests(db_project)
    ops.remove_dma(db_project, "USART1_TX", irq=False)
    out = tmp_path / "Out.ioc"
    db_project.save(out, backup=False)

    again = IocProject.load(out, device=db_project.device)
    assert again.doc.get("Dma.Request0") == "USART1_RX"
    assert again.doc.get("Dma.Request1") == "USART2_RX"
    assert again.doc.get("Dma.RequestsNb") == "2"
