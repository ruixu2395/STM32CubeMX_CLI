"""`cubemx_mcp.cubemx` —— 往返验证。

分两层：**比对器是纯函数**，用构造出来的文档就能把每个边界钉死；进程那一层用假
runner 测（真起一次 CubeMX 要几十秒，而且依赖本机装没装）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cubemx_mcp import cubemx
from cubemx_mcp.ioc.project import IocProject
from cubemx_mcp.ioc.raw import IocDocument


def doc(**entries: str) -> IocDocument:
    return IocDocument(dict(entries))


# ------------------------------------------------------------------ 比对器


def test_a_key_we_wrote_that_cubemx_drops_fails_the_check() -> None:
    """**这条是整个功能存在的理由。**

    写了键、校验也说没问题、CubeMX 却把它丢掉 —— 静默损坏，只有往返能发现。
    """
    baseline = doc(**{"Mcu.Name": "X"})
    candidate = doc(**{"Mcu.Name": "X", "USART2.IPParameters": "VirtualMode"})
    result = doc(**{"Mcu.Name": "X"})

    trip = cubemx.compare(candidate, result, baseline=baseline)

    assert trip.lost_ours == (("USART2.IPParameters", "VirtualMode"),)
    assert not trip.ok


def test_a_key_that_was_already_lossy_is_not_our_fault() -> None:
    """本来就丢的键（本次没碰）算旧账：报出来，但不挡住落盘。"""
    baseline = doc(**{"Mcu.Name": "X", "Legacy.K": "v"})
    candidate = doc(**{"Mcu.Name": "X", "Legacy.K": "v", "New.K": "1"})
    result = doc(**{"Mcu.Name": "X", "New.K": "1"})

    trip = cubemx.compare(candidate, result, baseline=baseline)

    assert trip.lost_ours == ()
    assert trip.lost_existing == (("Legacy.K", "v"),)
    assert trip.ok


def test_dropping_a_key_we_only_changed_still_counts_as_ours() -> None:
    """改了个已存在的键、CubeMX 把整行删了 —— 那也是我们造成的。

    只看"原来有没有这个键"会把这一类归进旧账，正好放过最该抓的一种。
    """
    baseline = doc(**{"NVIC.USART1_IRQn": "true:0:0"})
    candidate = doc(**{"NVIC.USART1_IRQn": "true:7:0"})
    result = doc()

    trip = cubemx.compare(candidate, result, baseline=baseline)

    assert trip.lost_ours == (("NVIC.USART1_IRQn", "true:7:0"),)
    assert not trip.ok


def test_cubemx_adding_defaults_is_only_informational() -> None:
    """CubeMX 补默认值/计算值是**合法**行为（实测：SPI2.Mode、CalculateBaudRate…）。"""
    baseline = doc(**{"Mcu.Name": "X"})
    candidate = doc(**{"Mcu.Name": "X"})
    result = doc(**{"Mcu.Name": "X", "SPI2.Mode": "SPI_MODE_MASTER"})

    trip = cubemx.compare(candidate, result, baseline=baseline)

    assert trip.added == (("SPI2.Mode", "SPI_MODE_MASTER"),)
    assert trip.ok


def test_project_manager_keys_are_ignored_and_said_so() -> None:
    """``saveas`` 必然改名 —— 报出来只会淹没真问题，但也不能当没看见。"""
    baseline = doc(**{"ProjectManager.ProjectName": "Old"})
    candidate = doc(**{"ProjectManager.ProjectName": "Old"})
    result = doc(**{"ProjectManager.ProjectName": "New"})

    trip = cubemx.compare(candidate, result, baseline=baseline)

    assert trip.changed == ()
    assert trip.ok
    assert any("ProjectManager.ProjectName" in n for n in trip.notes)


def test_without_a_baseline_everything_counts_as_ours() -> None:
    """独立的 `verify` 问的是"这份工程 CubeMX 收不收" —— 那任何键丢了都算数。"""
    candidate = doc(**{"Mcu.Name": "X"})
    result = doc(**{})

    trip = cubemx.compare(candidate, result)

    assert trip.lost_ours == (("Mcu.Name", "X"),)
    assert not trip.ok


def test_a_clean_round_trip_says_so() -> None:
    same = doc(**{"Mcu.Name": "X", "NVIC.SysTick_IRQn": "true:15:0"})
    trip = cubemx.compare(same, doc(**{"Mcu.Name": "X", "NVIC.SysTick_IRQn": "true:15:0"}))
    assert trip.ok and trip.render() == "CubeMX 往返：一个键都没丢，配置被完整接受。"


def test_values_are_compared_after_unescaping() -> None:
    """两边都比明文，不比转义态 —— 否则一个 ``\\:`` 的写法差异就报成"改过"。"""
    candidate = IocDocument({"NVIC.USART1_IRQn": "true\\:7\\:0"})
    result = IocDocument({"NVIC.USART1_IRQn": "true\\:7\\:0"})
    assert cubemx.compare(candidate, result).changed == ()


def test_the_report_lists_what_was_lost_first() -> None:
    baseline = doc(**{})
    candidate = doc(**{"A": "1", "B": "2"})
    result = doc(**{})
    text = cubemx.compare(candidate, result, baseline=baseline).render()
    assert text.index("本次写入的") < text.index("本来就会被丢掉") if "本来就会" in text else True
    assert "A = 1" in text and "B = 2" in text


def test_to_dict_is_json_shaped() -> None:
    baseline = doc(**{})
    candidate = doc(**{"A": "1"})
    trip = cubemx.compare(candidate, doc(**{}), baseline=baseline)
    data = trip.to_dict()
    assert data["ok"] is False
    assert data["lost_ours"] == [{"key": "A", "value": "1"}]
    assert set(data) == {"ok", "lost_ours", "lost_existing", "added", "changed", "notes"}


# ------------------------------------------------------------------ 进程层


class _FakeRunner:
    """假装跑了一次 CubeMX：按 `outcome` 决定写出什么。"""

    def __init__(self, outcome: str | None, text: str = "", stdout: str = "") -> None:
        self.outcome, self.text, self.stdout = outcome, text, stdout
        self.calls = 0

    def __call__(self, exe: Path, script: Path, timeout: float, tmp: Path) -> cubemx.RunResult:
        self.calls += 1
        # 脚本本身每次都该被真的写出来，顺手核一下形状
        body = script.read_text(encoding="utf-8")
        assert body.startswith("config load ") and body.rstrip().endswith("exit_mx")
        assert "\n\n" not in body.replace("\r\n", "\n")
        if self.outcome == "ok":
            (tmp / "out.ioc").write_text(self.text, encoding="utf-8", newline="")
            return cubemx.RunResult(returncode=0, stdout=self.stdout)
        if self.outcome == "timeout":
            return cubemx.RunResult(returncode=None, timed_out=True)
        if self.outcome == "exit":
            return cubemx.RunResult(returncode=1)
        if self.outcome == "empty":
            (tmp / "out.ioc").write_text("", encoding="utf-8")
            return cubemx.RunResult(returncode=0)
        if self.outcome == "garbage":
            # 第一行是 header（原样保留），所以"半截文件"至少得有两行才读不了
            (tmp / "out.ioc").write_text(
                "not an ioc\r\nnor is this\r\n", encoding="utf-8", newline=""
            )
            return cubemx.RunResult(returncode=0)
        return cubemx.RunResult(spawn_failed="no such file")


@pytest.fixture
def fake_exe(tmp_path: Path) -> Path:
    fake = tmp_path / "STM32CubeMX.exe"
    fake.write_bytes(b"stub")
    return fake


def _run(project: IocProject, exe: Path, runner) -> cubemx.Verification:
    return cubemx.verify(project, exe=exe, runner=runner)


def test_a_good_round_trip_reports_no_loss(project: IocProject, fake_exe: Path) -> None:
    result = _run(project, fake_exe, _FakeRunner("ok", text=project.render()))
    assert result.ran and result.round_trip.ok


def test_a_dropped_key_survives_all_the_way_to_the_verdict(
    project: IocProject, fake_exe: Path
) -> None:
    text = "\r\n".join(
        line for line in project.render().split("\r\n") if not line.startswith("USART1.")
    )
    result = _run(project, fake_exe, _FakeRunner("ok", text=text))
    assert result.ran and not result.round_trip.ok
    assert any(k.startswith("USART1.") for k, _ in result.round_trip.lost_ours)


def test_a_timeout_is_not_a_pass(project: IocProject, fake_exe: Path) -> None:
    result = _run(project, fake_exe, _FakeRunner("timeout"))
    assert not result.ran
    assert "强制终止" in result.failure


def test_a_nonzero_exit_is_not_a_pass(project: IocProject, fake_exe: Path) -> None:
    result = _run(project, fake_exe, _FakeRunner("exit"))
    assert not result.ran and "退出码" in result.failure


def test_an_empty_output_file_is_not_a_pass(project: IocProject, fake_exe: Path) -> None:
    result = _run(project, fake_exe, _FakeRunner("empty"))
    assert not result.ran and "没有写出结果文件" in result.failure


def test_a_garbage_output_file_is_not_a_pass(project: IocProject, fake_exe: Path) -> None:
    """CubeMX 写了个半截文件时，结论必须是"没验成"，不是"没有差异"。"""
    result = _run(project, fake_exe, _FakeRunner("garbage"))
    assert not result.ran and "读不了" in result.failure


def test_a_ko_line_is_not_a_pass(project: IocProject, fake_exe: Path) -> None:
    """上一轮实测 ``set gpio parameters`` 失败时 CubeMX 会回 ``KO``。"""
    result = _run(project, fake_exe, _FakeRunner("ok", text=project.render(), stdout="KO"))
    assert not result.ran and "报告了失败" in result.failure


def test_ko_inside_a_word_is_not_a_failure(project: IocProject, fake_exe: Path) -> None:
    """保守匹配：整行以 KO 开头才算，回显里带 KO 的路径不能误杀。"""
    result = _run(
        project, fake_exe,
        _FakeRunner("ok", text=project.render(), stdout="config load C:\\KOs\\x.ioc"),
    )
    assert result.ran


def test_a_failed_spawn_is_not_a_pass(project: IocProject, fake_exe: Path) -> None:
    result = _run(project, fake_exe, _FakeRunner("spawn"))
    assert not result.ran and "起不了进程" in result.failure


def test_no_cubemx_is_reported_never_silently_passed(project: IocProject) -> None:
    """**绝不假装验过了。** 和 `validate` 没有数据库时标 `skipped` 是同一条纪律。"""
    result = cubemx.verify(project, exe=None)
    assert not result.ran
    assert "ST_CUBEMX_EXE" in result.failure
    assert "config --set-cube-mx" in result.failure


def test_a_missing_exe_path_is_not_a_pass(project: IocProject, tmp_path: Path) -> None:
    result = cubemx.verify(project, exe=tmp_path / "nope.exe")
    assert not result.ran


def test_the_temp_directory_is_cleaned_up_on_success(
    project: IocProject, fake_exe: Path
) -> None:
    seen: list[Path] = []

    class Recorder(_FakeRunner):
        def __call__(self, exe, script, timeout, tmp):
            seen.append(tmp)
            return super().__call__(exe, script, timeout, tmp)

    _run(project, fake_exe, Recorder("ok", text=project.render()))
    assert seen and not seen[0].exists()


def test_the_temp_directory_is_cleaned_up_on_failure(
    project: IocProject, fake_exe: Path
) -> None:
    seen: list[Path] = []

    class Recorder(_FakeRunner):
        def __call__(self, exe, script, timeout, tmp):
            seen.append(tmp)
            return super().__call__(exe, script, timeout, tmp)

    _run(project, fake_exe, Recorder("timeout"))
    assert seen and not seen[0].exists()


def test_keep_temp_leaves_the_evidence_behind(
    project: IocProject, fake_exe: Path, capsys
) -> None:
    """报告看不懂时，临时的 in/out.ioc 就是证据 —— 留一份比重跑一次便宜。"""
    seen: list[Path] = []

    class Recorder(_FakeRunner):
        def __call__(self, exe, script, timeout, tmp):
            seen.append(tmp)
            return super().__call__(exe, script, timeout, tmp)

    try:
        cubemx.verify(project, exe=fake_exe, runner=Recorder("ok", text=project.render()), keep=True)
        assert seen and seen[0].exists()
        assert "--keep-temp" in capsys.readouterr().err
    finally:
        import shutil

        if seen:
            shutil.rmtree(seen[0], ignore_errors=True)


def test_the_candidate_goes_to_a_copy_never_to_the_original(
    project: IocProject, tmp_ioc: Path, fake_exe: Path
) -> None:
    """验证全程不碰原文件 —— "验证不过就不落盘"靠的就是这一点。"""
    before = tmp_ioc.read_bytes()
    _run(project, fake_exe, _FakeRunner("ok", text=""))
    assert tmp_ioc.read_bytes() == before
