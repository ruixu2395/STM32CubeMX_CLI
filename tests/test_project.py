"""`cubemx_mcp.ioc.project` —— 活视图与派生键维护。"""

from __future__ import annotations

from pathlib import Path

import pytest

from cubemx_mcp.ioc.raw import IocDocument
from cubemx_mcp.ioc.project import IocProject, is_virtual_pin


# ------------------------------------------------------------------ 往返


def test_roundtrip_is_byte_identical(sample_path: Path):
    """未改动的工程写回去必须一模一样。

    这是整个工具的安全底线：只要往返有偏差，就意味着"没碰过的键被改动了"。
    """
    original = sample_path.read_bytes()
    project = IocProject.load(sample_path)
    assert project.render().encode("utf-8") == original


def test_roundtrip_preserves_crlf_and_no_bom(sample_path: Path, tmp_path: Path):
    target = tmp_path / "out.ioc"
    target.write_bytes(sample_path.read_bytes())
    IocProject.load(target).save(target, backup=False)
    data = target.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")


# ------------------------------------------------------------------ 引脚


def test_pins_read_from_mcu_pin_index(project: IocProject):
    assert project.pin_names() == ("PA9", "PA10")
    assert [p.signal for p in project.pins()] == ["USART1_TX", "USART1_RX"]
    assert project.find_pin("PA9").mode == "Asynchronous"
    assert project.find_pin("PA9").locked is True


def test_find_pin_normalizes_case(project: IocProject):
    assert project.find_pin("pa9") is not None
    assert project.find_pin("PA99") is None


def test_add_and_remove_pin_keeps_index_dense(project: IocProject):
    project.add_pin("PB0")
    project.add_pin("PB1")
    project.remove_pin("PA10")

    assert project.pin_names() == ("PA9", "PB0", "PB1")
    assert project.doc.get("Mcu.PinsNb") == "3"
    # 不留空洞：Mcu.Pin0..2 连续
    assert [project.doc.get(f"Mcu.Pin{i}") for i in range(3)] == ["PA9", "PB0", "PB1"]
    assert project.doc.get("Mcu.Pin3") is None


def test_remove_pin_clears_its_config_keys(project: IocProject):
    project.remove_pin("PA9")
    assert project.doc.get("PA9.Signal") is None
    assert project.doc.get("PA9.Mode") is None


def test_view_is_live_not_a_copy(project: IocProject):
    """视图不持有副本：改了视图，文档立刻就变了。"""
    pin = project.find_pin("PA9")
    pin.signal = "GPIO_Output"
    assert project.doc.get("PA9.Signal") == "GPIO_Output"


# ------------------------------------------------------------------ GPIO 参数登记表


def test_label_is_registered_in_gpioparameters(project: IocProject):
    """**这是 CubeMX 认不认这一行的开关。**

    实测：只写 ``PB0.GPIO_Label`` 而不登记 ``PB0.GPIOParameters=GPIO_Label``，
    CubeMX 加载后会静默丢掉那一行。
    """
    project.add_pin("PB0")
    pin = project.find_pin("PB0")
    pin.signal = "GPIO_Output"
    pin.label = "LED"

    assert project.doc.get("PB0.GPIO_Label") == "LED"
    assert pin.parameters == ("GPIO_Label",)


def test_structural_keys_are_not_registered(project: IocProject):
    """``Signal`` / ``Mode`` / ``Locked`` 不进登记表（CubeMX 自己会补）。"""
    project.add_pin("PB0")
    pin = project.find_pin("PB0")
    pin.signal = "GPIO_Output"
    pin.mode = "Output"
    pin.locked = True
    assert "Signal" not in pin.parameters
    assert "Mode" not in pin.parameters
    assert "Locked" not in pin.parameters


def test_setting_property_to_none_deregisters_it(project: IocProject):
    project.add_pin("PB0")
    pin = project.find_pin("PB0")
    pin.set_property("GPIO_PuPd", "GPIO_NOPULL")
    assert pin.parameters == ("GPIO_PuPd",)

    pin.set_property("GPIO_PuPd", None)
    assert project.doc.get("PB0.GPIO_PuPd") is None
    assert project.doc.get("PB0.GPIOParameters") is None


def test_properties_excludes_top_level_fields(project: IocProject):
    project.add_pin("PB0")
    pin = project.find_pin("PB0")
    pin.signal = "GPIO_Output"
    pin.label = "LED"
    pin.set_property("GPIO_Speed", "GPIO_SPEED_FREQ_LOW")

    assert pin.properties == {"GPIO_Speed": "GPIO_SPEED_FREQ_LOW"}
    assert set(pin.values) == {
        "Signal", "GPIO_Label", "GPIO_Speed",
    }  # values 含顶层字段，不含登记表本身


# ------------------------------------------------------------------ 外设


def test_peripheral_virtual_key(project: IocProject):
    usart = project.find_peripheral("USART1")
    assert usart.virtual == ("VirtualMode", "VM_ASYNC")
    assert usart.virtual_mode == "VM_ASYNC"
    assert usart.parameters == ("VirtualMode",)


def test_set_virtual_clears_the_other_key(project: IocProject):
    """``VirtualMode`` 与 ``VirtualType`` 互斥 —— SPI 用后者、别的用前者。"""
    per = project.add_peripheral("SPI2")
    per.set_virtual("VirtualType", "VM_MASTER")
    assert per.virtual_type == "VM_MASTER"
    assert per.virtual_mode is None

    per.set_virtual("VirtualMode", "VM_SLAVE")
    assert per.virtual_type is None
    assert per.virtual_mode == "VM_SLAVE"
    assert "VirtualType" not in per.parameters
    assert "VirtualMode" in per.parameters


def test_set_param_registers_into_ipparameters(project: IocProject):
    """没登记进 ``IPParameters`` 的参数 CubeMX 打开工程时会无声丢掉。"""
    usart = project.find_peripheral("USART1")
    usart.set_param("BaudRate", "115200")
    assert project.doc.get("USART1.BaudRate") == "115200"
    assert set(usart.parameters) == {"VirtualMode", "BaudRate"}


def test_remove_peripheral_takes_its_keys_with_it(project: IocProject):
    project.remove_peripheral("USART1")
    assert "USART1" not in project.peripheral_names()
    assert project.doc.get("USART1.VirtualMode") is None


def test_ip_index_is_alphabetical(project: IocProject):
    """``Mcu.IP*`` 是字母序 —— 实测 ``DMA, I2C1, NVIC, RCC, SYS, USART1``。"""
    project.add_peripheral("SPI2")
    project.add_peripheral("I2C1")
    assert project.peripheral_names() == ("DMA", "I2C1", "NVIC", "RCC", "SPI2", "USART1")


# ------------------------------------------------------------------ 虚拟引脚


def test_virtual_pin_detection():
    assert is_virtual_pin("VP_SYS_VS_Systick")
    assert not is_virtual_pin("PA9")


def test_virtual_pin_roundtrips(project: IocProject):
    """``VP_SYS_VS_Systick`` 这类不占真实引脚，名字也不符合 ``P[A-Z]\\d+``。

    加了数据库时它在引脚清单里排最后（无封装位号）；排序逻辑见
    ``test_reindex_pins_puts_virtual_last``。
    """
    vp = project.add_pin("VP_SYS_VS_Systick")
    vp.signal = "SYS_VS_Systick"
    vp.mode = "SysTick"
    assert project.find_pin("VP_SYS_VS_Systick") is not None
    assert "VP_SYS_VS_Systick" in project.pin_names()


# ------------------------------------------------------------------ 落盘


def test_save_backs_up_first(project: IocProject, tmp_ioc: Path):
    """备份里存的必须是**改之前**的字节 —— 这才是"留得回退点"。"""
    before = tmp_ioc.read_bytes()
    project.add_pin("PB0")
    project.save()
    backups = list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    assert tmp_ioc.read_bytes() != before


def test_backups_in_the_same_second_do_not_overwrite_each_other(
    project: IocProject, tmp_ioc: Path
):
    """同一秒内的两次落盘要留**两份**备份。

    备份名的时间戳只到秒，早先的实现会让第二次 ``write_bytes`` 直接盖掉第一次，
    于是"每次写入都有回退点"悄悄不成立 —— 而 AI 连续改的时候同一秒两次是常态。
    每份备份还必须各自对应它落盘前那一刻的内容。
    """
    first = tmp_ioc.read_bytes()

    project.add_pin("PB0")
    project.save()
    second = tmp_ioc.read_bytes()
    assert second != first

    project.add_pin("PB1")
    project.save()

    backups = sorted(tmp_ioc.parent.glob("Proj.ioc.bak-*"))
    assert len(backups) == 2, [b.name for b in backups]
    assert [b.read_bytes() for b in backups] == [first, second]


def test_save_no_backup_flag(project: IocProject, tmp_ioc: Path):
    project.add_pin("PB0")
    project.save(backup=False)
    assert not list(tmp_ioc.parent.glob("Proj.ioc.bak-*"))


def test_save_leaves_no_temp_files(project: IocProject, tmp_ioc: Path):
    project.add_pin("PB0")
    project.save()
    assert not list(tmp_ioc.parent.glob("*.tmp"))


def test_passthrough_keys_are_kept(project: IocProject):
    """不归本项目管理的键必须原样留着 —— ProjectManager/CAD/PCC 等。"""
    keys = set(project.passthrough_keys())
    assert "ProjectManager.ProjectName" in keys
    assert "CAD.provider" in keys
    assert "PCC.Display" in keys
    assert "board" in keys
    # 反过来：已登记外设的键不叫"透传"（NVIC 就在 Mcu.IP* 里）
    assert "NVIC.USART1_IRQn" not in keys
    assert "USART1.VirtualMode" not in keys
    assert "PA9.Signal" not in keys

    project.add_pin("PB0")
    project.add_peripheral("SPI2")
    assert project.doc.get("ProjectManager.ProjectName") == "Usb_uart"
    assert project.doc.get("CAD.provider") is not None


def test_unknown_keys_survive_a_full_edit_cycle(project: IocProject, tmp_ioc: Path):
    project.doc.set("Weird.FutureKey", "keep me")
    project.add_pin("PB0")
    project.remove_pin("PA10")
    project.save()
    reloaded = IocProject.load(tmp_ioc)
    assert reloaded.doc.get("Weird.FutureKey") == "keep me"


def test_render_sorts_keys(sample_path: Path):
    project = IocProject.load(sample_path)
    keys = [
        line.split("=", 1)[0]
        for line in project.render().split("\r\n")[1:]
        if "=" in line
    ]
    assert keys == sorted(keys)


def test_reindex_pins_uses_package_position(db_project: IocProject):
    """``Mcu.Pin*`` 按**封装位号**排序，不是加入顺序也不是字母序。

    实测位号 PC13=7、PB0=46、PA9=101、PA10=102、PD5=119 —— 而字母序会把
    PA9/PA10 排到 PB0 前面，加入顺序又会给出 PB0,PC13,PD5。
    """
    db_project.add_pin("PB0")   # 位号 46
    db_project.add_pin("PC13")  # 位号 7
    db_project.add_pin("PD5")   # 位号 119

    assert db_project.pin_names() == ("PC13-ANTI_TAMP", "PB0", "PA9", "PA10", "PD5")


def test_add_pin_completes_the_name_from_the_db(db_project: IocProject):
    """``add_pin("PC13")`` 必须登记成库里那个名字，否则 CubeMX 不认这个脚。"""
    db_project.add_pin("PC13")
    assert db_project.pin_names() == ("PC13-ANTI_TAMP", "PA9", "PA10")
    assert db_project.find_pin("PC13").name == "PC13-ANTI_TAMP"


def test_add_pin_keeps_virtual_pin_case(project: IocProject):
    """``VP_SYS_VS_Systick`` 大小写敏感 —— 大写掉就不是同一个虚拟引脚了。"""
    project.add_pin("VP_SYS_VS_Systick")
    assert project.pin_names() == ("PA9", "PA10", "VP_SYS_VS_Systick")
