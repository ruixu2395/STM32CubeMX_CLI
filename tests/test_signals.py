import pytest

from cubemx_mcp.ioc.signals import (
    is_gpio_signal,
    is_pin_name,
    normalize_pin,
    parse_pin,
    signal_to_ip,
)


@pytest.mark.parametrize(
    "signal,expected",
    [
        ("USART1_TX", "USART1"),
        ("USART1_RX", "USART1"),
        ("UART4_TX", "UART4"),
        ("I2C1_SCL", "I2C1"),
        ("SPI1_NSS", "SPI1"),
        ("CAN1_RX", "CAN1"),
        ("TIM1_CH2", "TIM1"),
        ("TIM8_CH1N", "TIM8"),
        ("ADC1_IN0", "ADC1"),
        ("ADC1_EXTI11", "ADC1"),
        ("DCMI_D0", "DCMI"),
        ("SDIO_D0", "SDIO"),
        ("FMC_A0", "FMC"),
        ("LTDC_R0", "LTDC"),
        ("SAI1_SD_A", "SAI1"),
        ("RCC_OSC_IN", "RCC"),
        ("RCC_MCO_1", "RCC"),
        # 外设名自身带下划线，不能把 FS/HS 当成功能尾段剥掉
        ("USB_OTG_FS_VBUS", "USB_OTG_FS"),
        ("USB_OTG_FS_ID", "USB_OTG_FS"),
        ("USB_OTG_HS_ULPI_D0", "USB_OTG_HS"),
        ("ETH_RMII_REF_CLK", "ETH"),
        ("ETH_MDIO", "ETH"),
        ("QUADSPI_BK1_IO0", "QUADSPI"),
        # 功能尾段里带连字符
        ("SYS_JTCK-SWCLK", "SYS"),
        ("SYS_JTMS-SWDIO", "SYS"),
    ],
)
def test_signal_to_ip_heuristic(signal, expected):
    assert signal_to_ip(signal) == expected


def test_gpio_signals_have_no_peripheral():
    for signal in ("GPIO_Input", "GPIO_Output", "GPIO_Analog", "GPIO_EXTI13"):
        assert signal_to_ip(signal) is None
        assert is_gpio_signal(signal)


def test_known_ips_take_precedence():
    """给了权威外设名就走精确匹配，不再靠启发式猜。"""
    ips = {"USART1", "USART2", "USB_OTG_FS"}

    # 启发式会把 FMC_SDNRAS 当成整个外设名；known_ips 能纠回来
    assert signal_to_ip("FMC_SDNRAS") == "FMC_SDNRAS"
    assert signal_to_ip("FMC_SDNRAS", {"FMC"}) == "FMC"

    assert signal_to_ip("USART1_TX", ips) == "USART1"
    assert signal_to_ip("USB_OTG_FS_VBUS", ips) == "USB_OTG_FS"


def test_known_ips_does_not_prefix_confuse():
    """USART1 不应匹配到 USART10_TX 上。"""
    assert signal_to_ip("USART10_TX", {"USART1"}) == "USART10"


@pytest.mark.parametrize("name,expected", [("PA9", True), ("PA10", True), ("PC13", True), ("pa9", False), ("P9", False), ("A9", False)])
def test_is_pin_name(name, expected):
    assert is_pin_name(name) is expected


def test_parse_pin():
    assert parse_pin("PA9") == ("A", 9)
    assert parse_pin("PC13") == ("C", 13)


def test_normalize_pin():
    assert normalize_pin(" pa9 ") == "PA9"
    with pytest.raises(ValueError):
        normalize_pin("P9")
