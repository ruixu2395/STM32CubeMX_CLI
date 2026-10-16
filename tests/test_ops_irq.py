"""`ops.enable_irq` / `ops.disable_irq`。

这一组测试钉的是**元组尾巴**那件事：CubeMX 的 ``NVIC.<IRQn>`` 值是变长的，
第 3 段往上是它的版本印记（同一颗 MCU 的同一个中断名在不同版本的文件里都不一样），
所以已存在的键只能改前 3 段、尾巴逐字节带回去；不存在的键只写最小的三段式，
让 CubeMX 自己补。**猜错了会静默出错**，所以每条都拿真实往返实测过的形状当断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cubemx_mcp import ops
from cubemx_mcp.ioc.project import IocProject

#: 样本工程里 USART1_IRQn 的**原样**（6 段尾巴）。它证明了尾巴不是我们编得出来的东西。
USART1_TAIL = "\\:false\\:false\\:true\\:true\\:true\\:true"


def test_enabling_an_existing_irq_keeps_the_tail_byte_for_byte(
    db_project: IocProject,
) -> None:
    """已经开着的中断改优先级时，尾巴必须一个字节都不动。

    这是本步最核心的一条：尾巴是 CubeMX 的账本，我们不懂它，就不碰它。
    """
    before = db_project.doc.get_raw("NVIC.USART1_IRQn")
    assert before is not None and before.endswith(USART1_TAIL)

    ops.enable_irq(db_project, "USART1_IRQn", preemption=5)  # 分组 4 下子优先级恒 0

    after = db_project.doc.get_raw("NVIC.USART1_IRQn")
    assert after == "true\\:5\\:0" + USART1_TAIL


def test_enabling_a_new_irq_writes_the_minimal_three_fields(
    db_project: IocProject,
) -> None:
    """键不存在时只写三段式 —— **绝不自己编尾巴**。

    实测：写 ``true:7:0`` 进去，CubeMX 加载后会自己补成
    ``true:7:0:false:false:true:true:true:true``。自己编反而会写进 CubeMX 不认的账。
    """
    assert "NVIC.USART2_IRQn" not in db_project.doc

    ops.enable_irq(db_project, "USART2_IRQn", preemption=3)

    assert db_project.doc.get_raw("NVIC.USART2_IRQn") == "true\\:3\\:0"


def test_enabling_without_priority_uses_zero(db_project: IocProject) -> None:
    ops.enable_irq(db_project, "USART2_IRQn")
    assert db_project.doc.get_raw("NVIC.USART2_IRQn") == "true\\:0\\:0"


def test_enabling_an_already_enabled_irq_is_a_no_op(db_project: IocProject) -> None:
    """幂等：已经是目标状态就返回空清单（`_apply` 会打"没有可做的改动。"）。"""
    before = db_project.doc.get_raw("NVIC.USART1_IRQn")
    assert ops.enable_irq(db_project, "USART1_IRQn", preemption=0, sub=0) == []
    assert db_project.doc.get_raw("NVIC.USART1_IRQn") == before


def test_disabling_an_irq_deletes_the_line(db_project: IocProject) -> None:
    """**"关"在 .ioc 里的真身是"没有这一行"**，不是写 ``false``。

    实测：手写 ``NVIC.X_IRQn=false:0:0:…`` 进去，CubeMX 加载后整行都被删掉。
    写 false 是绕一圈回到同一处，还多一个 CubeMX 自己都不认的中间态。
    """
    changes = ops.disable_irq(db_project, "USART1_IRQn")

    assert "NVIC.USART1_IRQn" not in db_project.doc
    assert changes and "USART1_IRQn" in changes[0]
    # 全文件都不该出现写成 false 的中断行
    assert not [
        k for k in db_project.doc.keys_with_prefix("NVIC.")
        if (db_project.doc.get(k) or "").startswith("false")
    ]


def test_disabling_an_irq_that_is_off_is_a_no_op(db_project: IocProject) -> None:
    assert ops.disable_irq(db_project, "USART2_IRQn") == []


def test_an_unknown_irq_name_lists_candidates(db_project: IocProject) -> None:
    with pytest.raises(ops.OpError) as exc:
        ops.enable_irq(db_project, "USART9_IRQn")
    assert "相近的有" in str(exc.value)
    assert "USART1_IRQn" in str(exc.value)


def test_a_typo_without_the_irqn_suffix_still_gets_candidates(
    db_project: IocProject,
) -> None:
    """``USART1`` 这种少写后缀的错法，靠"去掉 _IRQn 比干名"那档兜住。"""
    with pytest.raises(ops.OpError) as exc:
        ops.enable_irq(db_project, "USART1_IRQ")
    assert "相近的有" in str(exc.value)


def test_a_non_irq_nvic_key_is_rejected(db_project: IocProject) -> None:
    """``NVIC.PriorityGroup`` 是普通标量，不是中断 —— 打开它应该被拒。"""
    with pytest.raises(ops.OpError):
        ops.enable_irq(db_project, "PriorityGroup")


def test_priority_out_of_range_is_refused(db_project: IocProject) -> None:
    """分组 4 下子优先级只能是 0 —— 越界要拦住，并把合法区间说出来。"""
    with pytest.raises(ops.OpError) as exc:
        ops.enable_irq(db_project, "USART1_IRQn", sub=5)
    assert "0..0" in str(exc.value)


def test_priority_range_follows_priority_group(db_project: IocProject) -> None:
    """同一颗 MCU，换一个分组，合法区间就跟着变 —— 说明区间是查出来的，不是写死的。"""
    db_project.doc.set("NVIC.PriorityGroup", "NVIC_PRIORITYGROUP_2")
    ops.enable_irq(db_project, "USART1_IRQn", sub=3)  # GROUP_2 下子优先级 0..3 合法
    assert db_project.doc.get_raw("NVIC.USART1_IRQn").startswith("true\\:0\\:3")

    with pytest.raises(ops.OpError):
        ops.enable_irq(db_project, "USART1_IRQn", sub=4)  # 4 就出去了


def test_enable_irq_needs_a_device(project: IocProject) -> None:
    """没有数据库时不猜 —— 宁可报错，也不写一个没核对过名字的中断。"""
    with pytest.raises(ops.OpError) as exc:
        ops.enable_irq(project, "USART1_IRQn")
    assert "CubeMX 数据库" in str(exc.value)


def test_a_missing_irq_table_says_so_instead_of_lying(
    tmp_ioc: Path, device, monkeypatch
) -> None:
    """``irqs()`` 返回 None（这颗 MCU 没 NVIC 表）时报的是"缺表"，不是"没这个中断"。

    2123 颗 MCU 里只有 69 颗带中断表 —— 缺表是常态，把这两件事说成一件事会让
    AI 去改一个根本没错的名字。
    """
    monkeypatch.setattr(type(device), "irqs", lambda self: None)
    proj = IocProject.load(tmp_ioc, device=device)

    with pytest.raises(ops.OpError) as exc:
        ops.enable_irq(proj, "USART1_IRQn")
    assert "缺表" in str(exc.value)
    assert "没有中断 USART1_IRQn" not in str(exc.value)
