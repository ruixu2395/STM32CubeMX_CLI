import pytest

from cubemx_mcp.ioc.raw import IocDocument, IocFormatError


def test_load_then_dump_is_byte_identical(sample_ioc, tmp_path):
    """未经修改的文件必须原样写回——这是所有上层功能的地基。"""
    original = sample_ioc.read_bytes()

    doc = IocDocument.load(sample_ioc)
    out = tmp_path / "copy.ioc"
    doc.dump(out)

    assert out.read_bytes() == original


def test_detects_crlf_and_no_bom(sample_doc):
    assert sample_doc.newline == "\r\n"
    assert sample_doc.bom is False
    assert sample_doc.header.startswith("#MicroXplorer")


def test_keys_are_sorted_on_dump(sample_doc, tmp_path):
    out = tmp_path / "sorted.ioc"
    sample_doc.dump(out)
    keys = [line.split("=", 1)[0] for line in out.read_text(encoding="utf-8").splitlines()[1:]]
    assert keys == sorted(keys)


def test_unknown_keys_survive(sample_ioc, tmp_path):
    """CubeMX 的 schema 不公开，不认识的键必须原样保留。"""
    doc = IocDocument.load(sample_ioc)
    doc.set("Zzz.FutureThing", "42")

    first = tmp_path / "future.ioc"
    doc.dump(first)
    assert "Zzz.FutureThing=42" in first.read_text(encoding="utf-8")

    second = tmp_path / "again.ioc"
    IocDocument.load(first).dump(second)
    assert second.read_bytes() == first.read_bytes()


def test_colon_escapes_are_not_decoded_on_load(sample_doc):
    """entries 存转义态原值；只有 get() 才反转义。"""
    key = "NVIC.USART1_IRQn"
    assert r"\:" in sample_doc.entries[key]
    assert ":" in sample_doc.get(key)
    assert "\\" not in sample_doc.get(key)


def test_get_set_are_plaintext(sample_doc):
    sample_doc.set("PCC.Display", "Plot: All Steps")
    assert sample_doc.entries["PCC.Display"] == r"Plot\: All Steps"
    assert sample_doc.get("PCC.Display") == "Plot: All Steps"


def test_set_none_deletes(sample_doc):
    assert "KeepUserPlacement" in sample_doc
    sample_doc.set("KeepUserPlacement", None)
    assert "KeepUserPlacement" not in sample_doc


@pytest.mark.parametrize(
    "content,needle",
    [
        ("#h\r\nnot a kv line\r\n", "2"),
        ("#h\r\nA=1\r\nA=2\r\n", "3"),
        ("", "空"),
    ],
)
def test_malformed_input_reports_line(tmp_path, content, needle):
    """非法行必须报错并指出行号——静默跳过等于无声损坏工程。"""
    path = tmp_path / "bad.ioc"
    path.write_bytes(content.encode("utf-8"))
    with pytest.raises(IocFormatError) as exc:
        IocDocument.load(path)
    assert needle in str(exc.value)


def test_blank_lines_are_tolerated(tmp_path):
    path = tmp_path / "blank.ioc"
    path.write_bytes(b"#h\r\n\r\nA=1\r\n\r\n")
    doc = IocDocument.load(path)
    assert doc.entries == {"A": "1"}
