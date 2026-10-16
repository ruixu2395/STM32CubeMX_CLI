"""起 CubeMX 跑一次"加载-保存"往返，看它认不认我们写的东西。

`.ioc` 是 CubeMX 的私有格式，它的规矩**没有公开文档**：有些键它照单全收，有些键
它加载时静默丢掉（手写的 ``NVIC.IParameters`` 就是），还有些配置它一保存就裁剪
（外设一个引脚都没配时，整个外设连同它的 ``NVIC.<IRQn>`` 行一起消失）。这些都不是
错误，是**沉默**。

所以`validate` 查完数据库之后还需要这一层：让 CubeMX 亲自把候选工程读一遍、再存
一遍，然后逐键比对。它能回答的问题只有一个，但这一个足够重要：

    **"我刚写进去的键，CubeMX 还给我了吗？"**

**它查的不是"逐字节往返一致"。** 那条路走不通 —— 往返里 CubeMX 会合法地补默认值
（``SPI2.Mode`` / ``SPI2.CalculateBaudRate``）、改计算值、重排 ``functionlistsort``、
改写 ``ProjectManager.ProjectName``。把那些当差异报，真问题就被淹了。判据窄得多：
我们写进去的键还在就过，丢了就不过；它自己补的、改的只作提示。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .ioc.project import IocProject
from .ioc.raw import IocDocument

#: 脚本模式标志。脚本是"一行一条命令"的纯文本，见 `_write_script`。
_SCRIPT_FLAG = "-q"

#: 单次运行的上限秒数。**这是上限不是期望值** —— JVM 冷启动加加载一颗 MCU 的库
#: 通常几十秒。超了就连进程树一起杀掉，绝不无限等。
DEFAULT_TIMEOUT = 240.0

#: Windows 上不给子进程开控制台窗口。从 Agent / 服务里调用时，一闪而过的黑框
#: 既难看又会让人以为"这程序弹窗了"。
_CREATE_NO_WINDOW = 0x08000000

#: 临时目录里那两个文件名。定义在一处是因为有个**跨函数的耦合**：`verify` 写脚本
#: 时用它们，`run_script` 猜产物时也用它们。两边各写一份字面量的话，哪天改了一边
#: 就会变成"每次都报没写出结果文件"，而且看不出是哪儿错了。
_IN_NAME = "in.ioc"
_OUT_NAME = "out.ioc"

#: 等产物的轮询间隔（秒）。这个循环整轮只跑几十秒，判据又是"文件大小变没变"，
#: 所以间隔取小一点无妨 —— 它直接决定往返验证最坏多花多少空转。
_POLL = 0.4

#: 产物大小连续几次不变才算写完。**"文件存在"这一条不够**：CubeMX 是先建文件、
#: 再往里写，中间那一瞬能读到 0 字节的产物。2 次 ≈ 0.8 秒。
_STABLE_ROUNDS = 2

#: CubeMX 报错行的形态。**保守匹配**：整行（去空白后）以 KO / ERROR 开头。
#: 用 ``"KO" in line`` 那种宽松写法会把回显里带 KO 的路径判成失败，而"验证失败"
#: 的代价是让用户白跑一趟、还不敢落盘。
_KO_RE = re.compile(r"^(KO|ERROR)\b", re.IGNORECASE)

#: CubeMX 每次保存都会自己重写的键。它们记录的是"这个文件自己叫什么"和"该生成
#: 哪些 Init 函数"，与配置内容无关，**而且随 saveas 的目标名而变** —— 报出来只会
#: 淹没真问题。注意是"进 notes"而不是"假装没看见"。
_VOLATILE = frozenset(
    {
        "ProjectManager.ProjectFileName",
        "ProjectManager.ProjectName",
        "ProjectManager.functionlistsort",
    }
)


class CubeMxError(RuntimeError):
    """跑 CubeMX 这件事本身没成（没装、超时、没产出）。**不等于验证通过。**"""


@dataclass(frozen=True)
class RunResult:
    """一次脚本执行的结果。

    ``returncode`` 是**启动器**的退出码，不是 JVM 的 —— 见 `run_script`：壳是
    fork-and-exit 的，JVM 的退出码拿不到。所以它只能当"壳自己就没起对"的信号，
    不能当判据；判据是产物在不在、以及有没有 KO。
    """

    returncode: int | None = None
    timed_out: bool = False
    #: 起进程就失败了（exe 不存在、权限、被杀软拦下），此时其余字段无意义
    spawn_failed: str | None = None
    stdout: str = ""
    stderr: str = ""
    #: 这一轮给的时间上限（秒）。报错文案要用**实际值**，不能写死 DEFAULT_TIMEOUT ——
    #: `--verify-timeout` 传了 30 秒却被告知"超过 240 秒"是句假话。
    limit: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class RoundTrip:
    """候选工程过完 CubeMX 往返之后，**按危险程度排好序**的差异表。"""

    #: (键, 我们写的值) —— 本次写入的键，CubeMX 丢了。**最危险的一条。**
    lost_ours: tuple[tuple[str, str], ...] = ()
    #: (键, 值) —— 本来就有的键被丢了。本次命令没碰它，是旧账。
    lost_existing: tuple[tuple[str, str], ...] = ()
    #: (键, CubeMX 补的值) —— 默认值/计算键，无需处理
    added: tuple[tuple[str, str], ...] = ()
    #: (键, 我们写的, CubeMX 改成) —— 明确的变化
    changed: tuple[tuple[str, str, str], ...] = ()
    #: 看到但按"不是问题"处理了的（已知易变键等）—— **不静默吞**
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.lost_ours

    def render(self) -> str:
        lines: list[str] = []
        if self.lost_ours:
            lines.append(
                f"CubeMX 丢掉了 {len(self.lost_ours)} 个**本次写入的**键"
                "（它们不会出现在 CubeMX 保存的结果里）："
            )
            lines += [f"  丢掉  {k} = {v}" for k, v in self.lost_ours]
        if self.lost_existing:
            lines.append(
                f"另有 {len(self.lost_existing)} 个键本来就会被丢掉（不是本次改动造成的）："
            )
            lines += [f"  丢掉  {k} = {v}" for k, v in self.lost_existing]
        if self.changed:
            lines.append(f"CubeMX 改了 {len(self.changed)} 个值：")
            lines += [f"  改    {k}  {a} → {b}" for k, a, b in self.changed]
        if self.added:
            lines.append(f"CubeMX 补了 {len(self.added)} 个默认值（无需处理）：")
            lines += [f"  补    {k} = {v}" for k, v in self.added[:12]]
            if len(self.added) > 12:
                lines.append(f"        …另有 {len(self.added) - 12} 个")
        if self.notes:
            lines.append("忽略：")
            lines += [f"  - {n}" for n in self.notes]
        if not lines:
            return "CubeMX 往返：一个键都没丢，配置被完整接受。"
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "lost_ours": [{"key": k, "value": v} for k, v in self.lost_ours],
            "lost_existing": [{"key": k, "value": v} for k, v in self.lost_existing],
            "added": [{"key": k, "value": v} for k, v in self.added],
            "changed": [{"key": k, "before": a, "after": b} for k, a, b in self.changed],
            "notes": list(self.notes),
        }


class Verification:
    """一次往返验证的**完整**结果：要么有结论，要么说清楚为什么没有。

    ``ran=False`` 时**绝不能对外说"通过"** —— 那和 `validate` 在没有数据库时把
    三层校验标成 ``skipped`` 是同一条纪律：没查就是没查。
    """

    def __init__(
        self,
        round_trip: RoundTrip | None = None,
        *,
        failure: str | None = None,
        detail: str = "",
    ) -> None:
        self.round_trip = round_trip
        self.failure = failure
        self.detail = detail

    @property
    def ran(self) -> bool:
        return self.round_trip is not None


# ------------------------------------------------------------------ 比对（纯函数）


def compare(
    candidate: IocDocument,
    result: IocDocument,
    *,
    baseline: IocDocument | None = None,
) -> RoundTrip:
    """三份文档进、一份差异表出。**纯函数**：不碰磁盘、不起进程。

    这条边界是刻意的 —— 归属逻辑（"丢的是不是我们写的"）是这块最容易出错的地方，
    做成纯函数就能用构造出来的文档把每个边界钉死，不必依赖本机装没装 CubeMX。

    ``baseline`` 是**改动前**的那份。有它才能把"被丢掉的键"拆成两类：
    我们写进去的（危险，要去修）和本来就丢的（旧账，提一句就行）。
    没有 baseline 时**整个文件都算"我们要的"** —— 独立的 `verify` 命令问的就是
    "这份工程 CubeMX 收不收"，那里的答案就该是"任何键丢了都算数"。
    """
    ours = {k: (candidate.get(k) or "") for k in candidate.entries}
    theirs = {k: (result.get(k) or "") for k in result.entries}

    if baseline is None:
        touched = set(ours)
    else:
        base = {k: (baseline.get(k) or "") for k in baseline.entries}
        # "碰过的" = 新增的 + 改过值的。**不能只看"原来没有"** —— 改一个已存在的
        # 键（比如把 USART1_IRQn 的优先级改掉）之后 CubeMX 把整行删了，那也是我们
        # 造成的，而归到"旧账"里就正好放过了最该抓的那一类。
        touched = {k for k in ours if k not in base or base[k] != ours[k]}

    lost_ours: list[tuple[str, str]] = []
    lost_existing: list[tuple[str, str]] = []
    added: list[tuple[str, str]] = []
    changed: list[tuple[str, str, str]] = []
    notes: list[str] = []

    for key in sorted(set(ours) - set(theirs)):
        if key in _VOLATILE:
            notes.append(f"{key}（CubeMX 自己重写的，忽略）")
            continue
        (lost_ours if key in touched else lost_existing).append((key, ours[key]))

    for key in sorted(set(theirs) - set(ours)):
        if key in _VOLATILE:
            continue
        added.append((key, theirs[key]))

    for key in sorted(set(ours) & set(theirs)):
        if ours[key] == theirs[key]:
            continue
        if key in _VOLATILE:
            # 已知易变键**变了**要报出来，不能因为它在 _VOLATILE 里就装作没看见
            notes.append(f"{key}：{ours[key]!r} → {theirs[key]!r}（CubeMX 自己重写的）")
            continue
        changed.append((key, ours[key], theirs[key]))

    return RoundTrip(
        tuple(lost_ours), tuple(lost_existing), tuple(added), tuple(changed), tuple(notes)
    )


# ------------------------------------------------------------------ 进程调用

Runner = Callable[[Path, Path, float, Path], RunResult]


def _clean_temp_root(exe: Path) -> Path:
    """找一个**路径里既没有空格、也没有非 ASCII 字符**的临时根目录。

    CubeMX 的脚本解析器是 Java 写的、行为不公开：``config load`` 后面那串能不能带
    空格、引号算不算界符、非 ASCII 路径按哪个编码解释，全都只能实测。**我们不赌** ——
    反正候选工程本来就要复制一份再喂给它，那就把复制品放到一条干净路径上，
    用户自己的路径（``C:\\我的 工程\\x.ioc``）根本不会出现在脚本里。
    """
    roots = [Path(tempfile.gettempdir())]
    system_root = os.environ.get("SystemRoot")
    if system_root:
        roots.append(Path(system_root) / "Temp")
    roots += [exe.parent, Path.cwd()]
    for root in roots:
        try:
            resolved = str(root.resolve())
        except OSError:
            continue
        if root.is_dir() and resolved.isascii() and " " not in resolved:
            return root
    raise CubeMxError(
        "找不到一个路径里没有空格/非 ASCII 字符的临时目录（试过："
        + "、".join(str(r) for r in roots)
        + "）。CubeMX 的脚本解析器对这类路径的行为没有实测过，所以这里不猜 —— "
        "把 TMP 指到一个干净目录再试。"
    )


def _write_script(path: Path, source: Path, target: Path) -> None:
    """脚本三行，一行一条命令，绝对路径，``exit_mx`` 收尾。

    用 ``exit_mx`` 而不是 ``exit``：后者在脚本模式下有已知的挂起问题。
    """
    body = "\r\n".join(
        [
            f"config load {source}",
            f"config saveas {target}",
            "exit_mx",
            "",
        ]
    )
    path.write_text(body, encoding="utf-8", newline="")


def _popen_kwargs(out, err) -> dict:
    """按平台分别构造。

    ``creationflags`` 在 POSIX 上直接抛 ``ValueError``，``start_new_session`` 在
    Windows 上不是合法参数 —— 两边都传一定会炸其中一个。
    """
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": err}
    if sys.platform == "win32":
        kw["creationflags"] = _CREATE_NO_WINDOW
    else:
        # 自成进程组，超时时才可能整组带走 —— POSIX 没有 taskkill /T 的等价物
        kw["start_new_session"] = True
    return kw


#: 标记允许的字符。临时目录名是我们自己用 ``mkdtemp`` 生成的（``[a-z0-9_]``），
#: 但 `_kill_tree` 要拿它拼进一条 shell 命令 —— 那就必须**先证明它只有这些字符**，
#: 而不是"反正是我们生成的"。这条纪律和 CLI 不把 AI 传的字符串拼进命令行是同一条。
_MARKER_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _kill_tree(proc: subprocess.Popen, marker: str) -> None:
    """连子孙一起杀，**包括已经脱了缰的 JVM**。

    两件事都要做：

    1. ``taskkill /F /T /PID`` —— 对付"壳还挂着"的情况，``/T`` 才会连子树。
       **必须在父进程还活着的时候调**：``/T`` 靠父 PID 反查子进程，父一死这层
       关系就没了。
    2. 按**命令行里的标记**扫一遍 —— 对付孤儿 JVM。``STM32CubeMX.exe`` 是 launch4j
       壳，它 fork 出 JVM 之后立刻自己退了（实测 0.03 秒、退出码 0、什么都不写），
       于是第 1 步**根本杀不到真正在干活的那个进程**。那个 JVM 会继续占着工程文件，
       让下一次验证莫名其妙地失败。

    标记用的是临时目录名（``cubemx-rt-<随机>``）：脚本路径是作为参数交给 JVM 的，
    所以它一定出现在 JVM 的命令行里（实测确认过）。用 `-like` 按进程名过滤掉
    ``javaw.exe`` / ``java.exe`` 之外的东西，**绝不按名字通杀** —— 用户自己开着的
    CubeMX 图形界面也是 javaw，那是别人的进程。
    """
    if sys.platform == "win32":
        if proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=15,
                    check=False,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                pass  # taskkill 不在 PATH / 被策略挡了，下面那步还在
        if _MARKER_RE.match(marker):
            script = (
                "Get-CimInstance Win32_Process "
                "-Filter \"Name='javaw.exe' or Name='java.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                "-ErrorAction SilentlyContinue }"
            )
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True,
                    timeout=30,
                    check=False,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if proc.poll() is None:
            proc.kill()
        return

    # POSIX：自成进程组，整组带走。POSIX 没有"父死了还能按 PID 找子进程"的等价物，
    # 所以 start_new_session 从一开始就是必需的（见 `_popen_kwargs`）。
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (OSError, ProcessLookupError):
        pass
    if proc.poll() is None:
        proc.kill()


def _decode(raw: bytes) -> str:
    """Java 往重定向的 stdout 写什么编码不保证（``-Dfile.encoding`` 管的是文件 I/O）。

    先按 UTF-8 试，出现替换字符就退回本地代码页。这段文本只用来**给人看失败原因**，
    判据不依赖它 —— 解错不会改结论。
    """
    text = raw.decode("utf-8", "replace")
    if "\ufffd" in text and sys.platform == "win32":
        try:
            alt = raw.decode("mbcs", "replace")
            if "\ufffd" not in alt:
                return alt
        except (LookupError, UnicodeDecodeError):
            pass
    return text


def _drain(stream, sink: list[bytes]) -> None:
    """把子进程的输出读进内存。**必须在后台线程里跑。**

    `Popen.wait()` 不会替你读管道：子进程写满管道的 64 KB 缓冲区就会**卡住写**，
    而我们还在等它退出 —— 教科书式的死锁。CubeMX 光启动日志就有十几 KB，
    离写满不远，所以这两根线不是"顺手收个日志"，是必需的。
    """
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            sink.append(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _wait_done(
    threads: list[threading.Thread], produced: Path, deadline: float
) -> bool:
    """等到"活干完了"。返回 ``False`` 表示超时。

    两个完成信号，缺一不可：

    1. **管道 EOF** —— 所有读线程结束，说明写端全关了。JVM 继承了我们 stdout/stderr
       的写端，所以**它退出时管道才会 EOF**。这是唯一精确的完成信号，
       也正是"壳退了不等于活干完了"这个坑的解药。
    2. **产物稳定** —— 万一哪天它不再继承句柄（换版本、换启动器），EOF 就永远不来。
       产物存在、非空、且连续 `_STABLE_ROUNDS` 次大小不变，也算完成。有它垫着，
       最坏情况是慢一点，而不是每次成功都白等满超时。
    """
    size, stable = -1, 0
    while True:
        if not any(t.is_alive() for t in threads):
            return True
        try:
            current = produced.stat().st_size
        except OSError:
            current = -1
        if current > 0 and current == size:
            stable += 1
            if stable >= _STABLE_ROUNDS:
                return True
        else:
            stable = 0
        size = current
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


def run_script(exe: Path, script: Path, timeout: float, tmp: Path) -> RunResult:
    """跑一次脚本。失败一律**如实返回**，绝不返回"看起来成功了"。

    **等的是 JVM，不是 ``STM32CubeMX.exe``。** 那个 exe 是个 launch4j 壳：实测
    ``-q`` 一起就返回（0.03 秒、退出码 0、输出文件还没有），真正的加载-保存在它
    fork 出去的 JVM 里继续跑二十来秒才写出结果。所以"进程退了"这个信号**在时间上
    早于产出**，照它判会把每一次成功的往返都判成失败 —— 见 `_wait_done` 里那两个
    真正管用的信号。

    也不用 ``subprocess.run(timeout=)``：它超时只 TerminateProcess 直接子进程
    （壳 —— 早没了），而且它的 Windows 超时分支紧接着无条件 ``communicate()``，
    管道写端还在 JVM 手里，那个调用会一直等不到 EOF，于是 CLI 卡死在超时之后。
    """
    kw = _popen_kwargs(subprocess.PIPE, subprocess.PIPE)
    # cwd 给安装目录：少数版本按相对路径找 db/ 和 jre/
    kw["cwd"] = str(exe.parent)
    try:
        proc = subprocess.Popen([str(exe), _SCRIPT_FLAG, str(script)], **kw)
    except OSError as exc:
        return RunResult(spawn_failed=str(exc), limit=timeout)

    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, out_chunks), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True),
    ]
    for thread in threads:
        thread.start()

    produced = tmp / _OUT_NAME
    deadline = time.monotonic() + timeout
    timed_out = not _wait_done(threads, produced, deadline)

    if timed_out:
        _kill_tree(proc, tmp.name)
        for thread in threads:
            thread.join(timeout=5)

    # 拿启动器的退出码。壳通常早退了（那就是它真正的退出码）；万一这个版本会等
    # JVM，产物都稳了，再给它几秒收尾。
    code = proc.poll()
    if code is None and not timed_out:
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree(proc, tmp.name)

    # 留证据（`--keep-temp` 时要能看）
    stdout = _decode(b"".join(out_chunks))
    stderr = _decode(b"".join(err_chunks))
    (tmp / "stdout.txt").write_bytes(b"".join(out_chunks))
    (tmp / "stderr.txt").write_bytes(b"".join(err_chunks))

    return RunResult(
        returncode=code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        limit=timeout,
    )


def _failure_of(run: RunResult, produced: Path) -> str:
    """这一轮为什么不算成功。**返回空串才算成功。**"""
    if run.spawn_failed:
        return f"起不了进程：{run.spawn_failed}"
    if run.timed_out:
        return f"超过 {run.limit:.0f} 秒没有结束，已强制终止"
    # 只在**真的看到**非零退出码时才报它：壳是 fork-and-exit 的，JVM 的退出码拿不到，
    # 所以这个字段本来就当不了判据（判据是产物和 KO 扫描）。``None``（拿不到）
    # 不该被当成失败 —— 那会把"没观察到"说成"观察到了坏结果"。
    if run.returncode:
        return f"退出码 {run.returncode}"
    if not produced.exists() or produced.stat().st_size == 0:
        # 最硬的一条：产物不在，前面几条就算都对也没意义
        return "CubeMX 没有写出结果文件（脚本可能没执行到 saveas）"
    for line in run.stdout.splitlines():
        if _KO_RE.match(line.strip()):
            return f"CubeMX 报告了失败：{line.strip()}"
    return ""


def _tail(text: str, limit: int = 2000) -> str:
    """CubeMX 的报错是 Java 异常栈，全打出来没用 —— 留尾巴就够定位了。"""
    text = text.strip()
    return text if len(text) <= limit else "…\n" + text[-limit:]


def _cleanup(tmp: Path, *, keep: bool) -> None:
    """删临时目录。**删不掉不算失败** —— 结论已经拿到了。

    重试是必需的：刚被 taskkill 带走的 JVM 可能还攥着文件句柄，Windows 上文件删除
    是"标记删除"，句柄一关就成，几毫秒的事。
    """
    if keep:
        print(f"（--keep-temp：临时目录留在 {tmp}）", file=sys.stderr)
        return
    for attempt in range(5):
        try:
            shutil.rmtree(tmp)
            return
        except OSError:
            time.sleep(0.2 * (attempt + 1))
    print(f"警告：临时目录删不掉（{tmp}），可手动删除。", file=sys.stderr)


# ------------------------------------------------------------------ 主入口


def verify(
    project: IocProject,
    *,
    exe: Path | None,
    timeout: float = DEFAULT_TIMEOUT,
    baseline: IocDocument | None = None,
    keep: bool = False,
    runner: Runner = run_script,
) -> Verification:
    """把候选工程交给 CubeMX 走一圈 load/save，回报它丢/补/改了什么。

    **全程不碰原文件**：候选渲染到临时目录里的一份副本上，往返在副本上做。
    所以"验证不过就不落盘"和既有的"校验不过就不落盘"是同一条保证，
    不需要任何新机制。

    ``runner`` 是给测试留的缝 —— 真的起一次 CubeMX 要几十秒，而这块的绝大部分
    逻辑（比对、归属、报告）不需要 JVM。
    """
    if exe is None or not Path(exe).exists():
        return Verification(
            failure=(
                "往返验证需要本机装有 STM32CubeMX，但没有找到它。"
                r"设 ST_CUBEMX_EXE 指向 STM32CubeMX.exe，"
                "或跑 cubemx-cli config --set-cube-mx <CubeMX 安装目录> 固定下来"
            )
        )
    exe = Path(exe)

    try:
        tmp = Path(tempfile.mkdtemp(prefix="cubemx-rt-", dir=_clean_temp_root(exe)))
    except (CubeMxError, OSError) as exc:
        return Verification(failure=str(exc))

    try:
        source, produced = tmp / _IN_NAME, tmp / _OUT_NAME
        # 从不存在的路径 saveas：这样"CubeMX 会不会拒绝覆盖"这个问题就不必回答
        source.write_text(project.render(), encoding="utf-8", newline="")
        script = tmp / "run.txt"
        _write_script(script, source, produced)

        run = runner(exe, script, timeout, tmp)
        reason = _failure_of(run, produced)
        if reason:
            return Verification(
                failure=reason,
                detail=_tail(run.stdout + ("\n" + run.stderr if run.stderr else "")),
            )

        try:
            result = IocDocument.load(produced)
        except Exception as exc:  # noqa: BLE001 - 半截文件也是"没验成"
            return Verification(failure=f"CubeMX 写出的文件读不了：{exc}")

        return Verification(compare(project.doc, result, baseline=baseline))
    finally:
        _cleanup(tmp, keep=keep)


__all__ = [
    "CubeMxError",
    "DEFAULT_TIMEOUT",
    "RoundTrip",
    "RunResult",
    "Runner",
    "Verification",
    "compare",
    "run_script",
    "verify",
]
