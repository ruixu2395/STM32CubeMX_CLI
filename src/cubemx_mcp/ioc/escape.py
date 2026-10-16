"""`.ioc` 值的转义与反转义。

CubeMX 在 `.ioc` 里把值中的 ``:`` 写成 ``\\:``，例如::

    PCC.Display=Plot\\: All Steps
    NVIC.USART1_IRQn=true\\:0\\:0\\:false\\:false\\:true\\:true\\:true\\:true

写回时不转义，CubeMX 会把这些值在冒号处截断。``=`` 同样处理，``\\`` 自身转义
为 ``\\\\``。
"""

_ESCAPE_MAP = (("\\", "\\\\"), (":", "\\:"), ("=", "\\="))


def escape(value: str) -> str:
    """明文 -> `.ioc` 文件中的转义形式。"""
    for src, dst in _ESCAPE_MAP:
        value = value.replace(src, dst)
    return value


def unescape(value: str) -> str:
    """`escape` 的逆运算：反斜杠后跟任意字符时，该字符被原样取用。

    未知的转义序列按「取下一个字符」处理，因此对 CubeMX 未来新增的转义形式
    也能安全降级，不会抛错。
    """
    if "\\" not in value:
        return value
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            out.append(value[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)
