# 失败诊断与恢复

**前提：校验没过时磁盘上的文件一个字节都没变，也不会留下备份。** 所以下面任何一条失败
都可以直接改命令重试，不需要先恢复文件。

报错一律走 stderr，前面带 `错误：`；退出码见 SKILL.md。**报错信息本身就是给你的建议** ——
它通常会列出合法取值，照着改就行，不用去翻数据库。

---

## `cubemx-cli` 命令找不到

```
$ cubemx-cli validate proj.ioc
bash: cubemx-cli: command not found
# Windows cmd / PowerShell 下是：'cubemx-cli' 不是内部或外部命令，也不是可运行的程序
```

**这一条几乎总会出现，而它不表示"这个环境没装工具"。** 它只表示用户没把 exe 加进
`PATH`。换 skill 根目录里那个继续干，别停在这里：

```bash
<skill 根目录>\cubemx-cli.exe validate proj.ioc
```

路径**拼完整**再调 —— 当前工作目录不保证在哪，写 `.claude/skills/...` 这种相对路径会指到
别处；带空格就加引号。下面文档里凡是写 `cubemx-cli` 的地方，你在走这条路时都整体换成
上面那个调用形式 —— 参数、输出、退出码完全一样。

**skill 根目录里也没有那个 exe** 时才真的没辙 —— **问用户要 exe 的路径**。别自己去装，
也别从源码跑：那会改动用户的环境，而跟用户要一个路径是最省事的。两条路的顺序见
SKILL.md 的「开始工作」。

## 最危险的一条：没数据库时 `validate` 会说"通过"

```bash
$ cubemx-cli validate proj.ioc --json
{
  "ok": true,
  "issues": [],
  "skipped": [
    "引脚能力（缺 CubeMX 数据库）",
    "模式自洽（缺 CubeMX 数据库）",
    "参数名与取值（缺 CubeMX 数据库）"
  ]
}
$ echo $?
0
```

**`ok: true` + 退出码 0，但三层校验根本没跑。** 只看退出码会得出"工程没问题"的错误结论。

所以：

1. 读 `--json` 时必须**同时看 `skipped`**，非空就等于"这些没查"。
2. 没有数据库时**不要**说"校验通过"，要说"三层校验被跳过，无法确认"。
3. 先自己把数据库配上再重跑，一条命令：

   ```bash
   cubemx-cli config --set-db "D:\stm32CubeMX"     # 安装目录或 db\mcu 都行
   ```

   这是非交互的，不会弹提示。**首次运行的交互式提示只在真终端里出现** —— 你用 Bash 工具
   调用时它自动跳过，所以别等它。配完重跑一次，确认 `skipped` 空了才算数。

   查不到 CubeMX 装在哪时，`cubemx-cli config` 的 `problems` 会说明每个源为什么被忽略
   （典型：用户把 `ST_CUBEMX_EXE` 拼错，于是自动探测被跳过）。

没数据库时意图级命令会直接拒绝：

```
错误：意图级配置需要 CubeMX 数据库来确认引脚能力。设置 ST_CUBEMX_DB 指向 ...
      或改用逐字段命令（set-pin / set-peripheral）。
```

逐字段命令仍然可用，但**没有任何能力校验** —— 引脚能不能承载这个信号全靠你自己判断，
务必让用户复核。

`list` 也没数据库时不会失败，但会**明说清单不全**：

```
STM32F407Z(E-G)Tx  58 个外设，已配置 1 个（缺数据库，这里只有工程里已声明过的外设）
```

那行字必须当真：没数据库时"未配置"那一段是**空的**，不代表这颗 MCU 没有别的外设。
`--json` 里对应 `"complete": false`。

## `list` 说某一页"看不了"

```
看不了：中断表、DMA 请求表（数据库里没有这份表，或格式不认识） —— 先跑 cubemx-cli config 确认数据库路径。
```

这**不是**"这个外设没有中断"，而是"这一页没查成"：整页不会打印（不会印成
"还可以开：无"）。`--json` 里对应页给 `null`，`missing` 里给页名：

```json
{"nvic": null, "dma": null, "missing": ["nvic", "dma"],
 "modes": [...], "params": [...]}
```

读得出来的页照常给 —— 一页读不了不等于整个外设看不了。`null` 和 `[]` 是两回事：
`[]` 表示**问了，确实是空的**；`null` 表示**没结论**。**别把"看不了"当成"没有"转述给用户。**

常见原因有两条：数据库路径不对；或者这颗 MCU 太新 —— 数据库里 2123 颗 MCU 只有
69 份 NVIC 表，缺表是**正常状态**，不是故障。

## 外设名 / 模式名错了

```
错误：这颗 MCU 上没有外设 USART9，相近的有：USART1, USART2, USART3, USART6
错误：USART2 没有模式 'Nonsense'。可选：Asynchronous, Synchronous, Half_duplex(...), ...
```

按列出的候选改。模式名可以用 `modes <工程> <外设> --json` 事先查全（含每个模式需要哪些信号）。

## 引脚问题

```
错误：PA9 无法承载 USART2_TX。可以用的脚：PA2, PD5
```

这就是问"我该用哪个脚"的最快方式 —— 报错把**全部**合法脚列出来了。

```
错误：PA9 当前属于 USART1（USART1_TX），要改成 USART2 请加 --force
```

这个脚被别的外设占了。**先问用户**，别默默加 `--force`：抢走一个已经配好的脚会让原来
那个外设缺信号（接着 `validate` 会报 `mode.missing_signals`）。

注意这里的 `--force` 和写完之后的 `--force` 是两回事：

| 位置 | 含义 |
|---|---|
| `set-pin ... --force` | 允许抢走别处已占用的引脚（**明确的操作意图**） |
| 任何写命令 `--force` | 校验有 error 也照样落盘（**绕过安全网**，默认不要用） |

## 参数问题

```
错误：参数要写成 名=值，收到的是 'BaudRate'
```
`--param BaudRate` 少了值。写 `--param BaudRate=115200`。

```
[error] param.not_in_mode USART1: USART1.NoSuchParam 不属于模式 Asynchronous 的参数
    建议：Asynchronous 允许：BaudRate, HwFlowCtl, Mode, OverSampling, Parity, StopBits, WordLength
```
参数名不是这个外设这个模式的合法参数。**建议那行就是全部合法值**。这类错误会挡住落盘
（退出码 2），别用 `--force` 硬塞 —— 塞进去的键 CubeMX 不认。

## 校验报错：分清"配错了"和"还没配完"

```
[error] mode.unknown PA2: PA2.Mode=Nonsense 不在 USART2 的模式库里     ← 配错了，挡住
[error] mode.missing_signals USART2: USART2 的模式 Asynchronous 需要 RX, TX，缺 RX  ← 没配完
[error] virtual.missing USART2: USART2 的模式是 Asynchronous，必须同时写 VirtualMode=VM_ASYNC
```

- **挡住落盘的那些**（退出码 2）：`mode.unknown` / `mode.inconsistent` /
  `pin.unsupported_signal` / `param.*` / `virtual.*` 之外的 …。必须修。
- **"还没配完"的三种**（`mode.missing` / `mode.missing_signals` / `virtual.missing`）：
  **允许落盘**，退出码 0，只在 stderr 提醒。逐字段分步配置必然路过这些状态。

每种 error 都带一行 `建议：`，直接给下一步命令。**照它做通常就配好了。**

```
    建议：补齐：cubemx-cli add-peripheral <工程> USART2 --mode Asynchronous
```

（`<工程>` 是占位符，换成实际路径。）

## `--verify` 说"CubeMX 丢掉了你写的键"

这是往返验证**唯一**的否决理由，也是最值得停下来看一眼的一条：

```
CubeMX 丢掉了 2 个**本次写入的**键（它们不会出现在 CubeMX 保存的结果里）：
  丢掉  USART1.VirtualMode = VM_ASYNC
  丢掉  NVIC.USART1_IRQn = true:5:0
```

**文件没有被写入**（退出码 2 —— 和校验没过是同一种退出码）。同一次报告里还会带上别的
几类，**它们不是否决理由**，别混在一起读：

| 报告里的分类 | 含义 | 要不要管 |
|---|---|---|
| `丢掉了 …本次写入的…` | **我们刚写的键** CubeMX 不收 | **要管**，就是它在否决 |
| `另有 N 个键本来就会被丢掉` | 本次命令没碰、CubeMX 也不收的旧账 | 不用管，但要知道 |
| `CubeMX 改了 N 个值` | 规范化（`Enable_Timer`→`Internal`、文件名等） | 不用管 |
| `CubeMX 补了 N 个默认值` | 它自己算出来的默认值/计算键 | 不用管 |
| `忽略：` | 已知易变的键（`ProjectManager.*` 等） | 不用管 |

### 丢的是"外设的键" → 那个外设根本没配好

最常见的一类，也是本工具最想替你拦住的：**给一个没配置好的外设开中断 / 挂 DMA，
CubeMX 加载时会把那个外设连同它的中断行一起清掉。** 典型是外设登记在 `Mcu.IP*` 里、
声明了模式，却**一个引脚都没有**。

先跑 `validate`，它会报（`irq.orphan` / `dma.orphan`）：

```
[warning] irq.orphan USART1_IRQn: USART1_IRQn 开给 USART1，但 USART1 一个引脚都没配 —— CubeMX 保存时会把外设和这一行一起删掉
    建议：先把 USART1 配好，或关掉它：cubemx-cli disable-irq <工程> USART1_IRQn
```

DMA 那一条的措辞不同，因为**后果不同** —— 它是"块会留着但永远不触发"，
不是"被删掉"。两条都是"先把 `owners` 那个外设配好"同一条修法。

**修法是先把外设配好，再开中断**（不要反过来去 `--force`）：

```bash
cubemx-cli add-peripheral proj.ioc USART1 --mode Asynchronous --tx PA9 --rx PA10
cubemx-cli enable-irq     proj.ioc USART1_IRQn --preemption 5 --verify
```

注意这类是 **warning、不挡落盘** —— 也就是说**不加 `--verify` 的话它会静默写进去**，
等下次 CubeMX 打开时那行才悄悄消失。DMA 那条更阴：块会**留着但变死**
（CubeMX 不裁剪孤立的 `Dma.*`），文件看着完全正常。

### 丢的是别的键 → 那个写法 CubeMX 不认

比如手写了一个它不认识的派生键。这时**别加 `--force`**（`--force` 绕过的是校验，
绕不过 CubeMX 自己的裁剪 —— 最终文件里还是没有那个键）。把报告里"丢掉的键"那份列表
交给用户，说明工具写不出这个键，并给出退路：**在 CubeMX GUI 里点一次**，
再回来看它写成了什么样。

### 报告看不懂 / 想留证据

```bash
cubemx-cli verify proj.ioc --json              # 结构化结果
cubemx-cli verify proj.ioc --keep-temp         # 留下临时目录（含 CubeMX 真正存出来的那份 .ioc）
```

`--keep-temp` 留下的是**证据**：临时目录里那份「CubeMX 存出来的 .ioc」可以直接和你的
工程对读，比报告更直接。

## `--verify` / `verify` 说"没跑成"

```
往返验证没跑成：往返验证需要本机装有 STM32CubeMX，但没有找到它。
             设 ST_CUBEMX_EXE 指向 STM32CubeMX.exe，
             或跑 cubemx-cli config --set-cube-mx <CubeMX 安装目录> 固定下来
```

（写命令上的 `--verify` 说的是"**往返验证没跑成，没有写入文件。**"，后半段同一句 ——
特意多那半句，因为它意味着这次改动**没落盘**。）

**"没跑成"不等于"验过了"。** 这一条绝不能转述成"应该没问题"：

| 退出码 | 情况 | 怎么办 |
|---|---|---|
| 1 | 找不到 `STM32CubeMX.exe` | 设 `ST_CUBEMX_EXE`，或 `cubemx-cli config --set-cube-mx <路径>`。注意 CLI 装在别的机器/容器里时必然如此 |
| 1 | 超时（默认 240 秒） | 加 `--verify-timeout 480`；还不行就是 CubeMX 卡住了，别指望它 |
| 1 | CubeMX 报了错 / 写了个半截文件 | 读报告尾部附上的输出（Java 异常栈的**尾部**才是有用那几行） |

只读的 `verify 工程.ioc` 跑不成时**退 1**（用法/环境问题），而不是 2 —— 2 永远只表示
"引擎说这份配置不行"，没跑起来不算。

没装 CubeMX 时**也可以完全不碰它**：`--verify` 是选配。但那么做就没法回答
"CubeMX 认不认这个写法"，交付时要如实说明这次**没有**验过。

## 改完没效果

本工具**只改 `.ioc` 文件，不生成代码**。改完要在 CubeMX 里重新打开工程（或
Generate Code）才会体现到代码里。这一点要跟用户说清楚。

另外 CubeMX 有可能在它自己保存时规范化掉一些东西 —— 如果用户在 GUI 里改了又存，建议
回来重跑一次 `cubemx-cli validate` 看有没有变化。

## 备份

每次**真正落盘**前会在同目录留一份 `<名字>.ioc.bak-<时间戳>`（同一秒内多次写入会自动
加 `-2`、`-3` 后缀，不会互相覆盖）。不要 `--no-backup`，除非用户明确要求。

回退就是把 `.bak-*` 复制回去 —— 但先问用户，不要在没确认的情况下覆盖他的工作。

## 仍然失败时

不要靠 `--force` 硬过，也不要手改 `.ioc`（那正是会静默出错的路）。把**完整的 stderr**
交给用户，并说明：卡在哪一步、工具建议了什么、为什么没照做。多数情况下是这颗 MCU 的
数据库里没有用户想要的那种配置 —— 这时要么换个外设实例，要么让用户确认后走逐字段命令
（并明确告知这次没有能力校验兜底）。
