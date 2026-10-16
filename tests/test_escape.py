import pytest

from cubemx_mcp.ioc.escape import escape, unescape


@pytest.mark.parametrize(
    "plain,encoded",
    [
        ("", ""),
        ("VM_ASYNC", "VM_ASYNC"),
        ("Plot: All Steps", r"Plot\: All Steps"),
        ("true:0:0:false", r"true\:0\:0\:false"),
        (r"a\b", r"a\\b"),
        ("a=b", r"a\=b"),
    ],
)
def test_escape(plain, encoded):
    assert escape(plain) == encoded
    assert unescape(encoded) == plain


@pytest.mark.parametrize(
    "plain",
    ["", "x", "a:b", r"a\b", "a=b", r"混合\路径:C=D", "多:个:冒:号"],
)
def test_roundtrip(plain):
    assert unescape(escape(plain)) == plain


def test_unescape_tolerates_unknown_escape():
    """我们没预料到的转义不应抛错，而是退化成"取下一个字符"。"""
    assert unescape(r"a\qb") == "aqb"
