---
name: cubemx-configure
description: "通过本地 cubemx-cli 读写 STM32CubeMX 工程（.ioc）：查看已配置的引脚与外设、为一个外设挑引脚定模式写全派生键、配置 GPIO、改参数、跑五层校验。适用于用户要求给 STM32 工程加串口/SPI/I2C/定时器、点亮某个 LED、核对引脚分配，或排查 .ioc 配置错误时。也适用于直接手改 .ioc 会静默出错、需要按 CubeMX 数据库校验的场合。"
license: MIT
compatibility: "需要 cubemx-cli。**PATH 里找不到 `cubemx-cli` 命令是常态** —— 那说明用户没把它加进环境变量，不代表没装：直接用 skill 根目录里的 cubemx-cli.exe 即可（见「开始工作」）。数据库（<STM32CubeMX>/db/mcu）决定引脚能力/模式/参数三层校验是否可用 —— 没配就自己跑 cubemx-cli config --set-db <CubeMX 安装目录> 配上（非交互，一条命令）；不要指望首次运行的交互式提示，那条只在真终端里出现。常规读写都不需要启动 CubeMX；只有显式加 --verify（或跑 verify 命令）时才会起一次 STM32CubeMX.exe 做加载-保存往返，需要 ST_CUBEMX_EXE 指向它，单次最长 240 秒。只读写本机文件，不联网。"
metadata:
  version: "0.1.0"
---

# STM32CubeMX 工程配置（.ioc）

`.ioc` 是 CubeMX 的工程文件。这个工具**直接改它**，不经过 CubeMX 进程 —— 所以不需要启动
JVM，几秒就能改完一个工程。

## 硬规则（先读这四条）

1. **绝对不要用 Edit/Write 手改 `.ioc`。** 见下方"为什么不能手改"，手写的键会被 CubeMX
   **静默丢弃** —— 文件看着对，CubeMX 打开却是空的。一切改动都走 `cubemx-cli`。
2. **写入前先 `--dry-run` 看 diff。** 每个写命令都支持，`--dry-run` 会打印改动清单和
   unified diff 但不碰文件。看过 diff 再去掉这个开关真正落盘。
3. **关中断 = 删掉那一行**，不是写 `false`。文件里"关着的"中断**根本没有那一行**。
   手写 `NVIC.USART2_IRQn=false:0:0` 是错的 —— CubeMX 下次保存时会把整行删掉，你以为
   写了个"关"，其实写了个要靠 CubeMX 收拾的垃圾。用 `disable-irq`，它做的就是删行。
4. **不要用 `--force` 绕过校验。** 校验报错就是配错了，改配置，不是改校验。`--force` 在
   两个位置含义不同，都别随手加：
   - `set-pin ... --force` = 允许**抢走**别处已占用的引脚。先问用户。
   - 写命令末尾的 `--force` = 有 error 也照样落盘，**绕过安全网**。只有在用户明确要求写
     一个 CubeMX 数据库不认识的自定义配置时才用。

## 为什么不能手改 .ioc

`.ioc` 里有两张**登记表**，键必须先登记进去，否则 CubeMX 加载时直接丢弃：

```
USART2.IPParameters=VirtualMode,BaudRate     ← 外设参数要登记
PC13.GPIOParameters=GPIO_Label,GPIO_PuPd     ← 引脚参数要登记
```

手写 `PC13.GPIO_Label=KEY` 而漏了登记表，CubeMX 打开时那一行就没了。这类错误**不会报错**，
只会让工程悄悄缺一块。工具会自动维护这两张表，手改不会。

## 开始工作：先确定这个环境里该用哪个 `cubemx-cli`

**按顺序试，前一条不通就试下一条：**

```bash
# ① PATH 里有就直接用
cubemx-cli --help        # 报 "command not found" / "不是内部或外部命令" 就试下一条

# ② 用本 skill 根目录里的 exe —— 就在 SKILL.md 旁边，随 skill 一起分发
<skill 根目录>\cubemx-cli.exe --help
```

**② 是常态，不是备用方案。** `cubemx-cli` 找不到，只说明**用户没把这个 exe 加进环境
变量**，不代表这个环境没装工具 —— exe 就躺在 skill 根目录里（和 SKILL.md 同一层），
功能一字不差。所以：

> **找不到 `cubemx-cli` 命令时，不要就此断言"环境里没有 cubemx-cli"或放弃，
> 直接换 ② 继续干。**

调 ② 时把路径**拼完整** —— 当前工作目录不保证在哪，写 `references/...` 那种相对路径会
指到别处；路径带空格就加引号。

**两条都不通**（skill 根目录里就是没有这个 exe）—— **问用户要 exe 的路径**，拿到后接着干。
**别自己去装，也别从源码跑**：该用哪个 exe 是用户的事，你的活是配 `.ioc`，不是修工具链。

下面所有命令都写作 `cubemx-cli`，**意思是"你上面选中的那个调用方式"** —— 走 ② 时就把每
一条里的 `cubemx-cli` 整体换成那个 exe 的完整路径。参数、输出、退出码完全一样。

**顺带一说**：② 那份 exe 读的是用户既有的配置文件，所以数据库路径和 `STM32CubeMX.exe`
的位置**开箱即用**，`--verify` 不需要额外设环境变量。

### 第一步：确认数据库

它决定三层校验能不能跑，而这三层正是这个工具存在的理由：

```bash
cubemx-cli config --json
```

`db_root` 是 `null`（`source` 为 `none`）说明没找到数据库 —— 引脚能力/模式/参数三层会**全部
跳过**，命令照样能跑但少了最重要的一道保险。这时**别默认"没数据库也能配好"**。

没配好就**自己配上**，一条非交互命令，别去问用户也别等提示：

```bash
cubemx-cli config --set-db "D:\stm32CubeMX"    # 安装目录或 db\mcu 目录都接受
```

**首次运行的交互式提示只在真终端里出现**（stdin 不是终端时自动跳过，不会卡住）——
你在 Bash 工具里调用它，它不弹提示，所以别指望那条路。

输出里出现 `problems` 时读一下：它点名叫出是哪个变量、哪个文件的问题（典型的是用户把
`ST_CUBEMX_EXE` 拼错了）。还要注意 **`ST_CUBEMX_DB` 环境变量优先级高于配置文件** ——
用户设了它的话，你存进配置的路径这次不生效，得如实告诉他。

## 命令

| 命令 | 用途 |
|---|---|
| `list <工程> [--json]` | **这颗 MCU 有哪些外设、各自配到哪一步**（要"有什么"从这里开始） |
| `list <工程> <外设> [--page mode\|param\|nvic\|dma\|gpio] [--json]` | **这个外设的配置页**：模式 / 参数 / 中断 / DMA 请求 / 引脚。只读 |
| `info <工程> --json` | MCU 型号、封装、已配置引脚数、外设清单 |
| `pins <工程> --json` | 已配置的引脚（名字/信号/模式/标签/属性） |
| `peripherals <工程> --json` | 外设 + 各自占用的引脚 |
| `modes <工程> <外设> --json` | **这个外设有哪些模式、各要哪些信号** —— 配错时的自救入口 |
| `validate <工程> [--json]` | 跑全部校验 |
| `show <工程>` | 原样打印文件（只在需要看原始键值时用） |
| `add-peripheral <工程> <外设> [--mode M] [--param K=V] [--<信号> 引脚]` | **意图级**：自动挑引脚、定模式、写全派生键 |
| `add-gpio <工程> <引脚> --mode Input\|Output\|Analog\|EXTI [--label L]` | **意图级**：配 GPIO |
| `set-pin <工程> <引脚> [--signal S] [--mode M] [--label L] [--lock/--unlock] [--set K=V]` | 逐字段 |
| `set-peripheral <工程> <外设> [--virtual-mode V \| --virtual-type V] [--param K=V] [--unset K]` | 逐字段 |
| `clear-pin <工程> <引脚>` | 清掉引脚的配置 |
| `remove-peripheral <工程> <外设> [--keep-pins]` | 移除外设（默认连引脚一起清） |
| `enable-irq <工程> <IRQ名> [--preemption N] [--sub N]` | **开中断**（已开着就只改优先级） |
| `disable-irq <工程> <IRQ名>` | **关中断**（= 删掉那一行，见硬规则 3） |
| `add-dma <工程> <请求名> [--stream S] [--param K=V] [--no-irq] [--preemption N]` | **挂 DMA**（默认自动挑通道，并顺带开流中断） |
| `remove-dma <工程> <请求名> [--keep-irq]` | **摘 DMA**（后面的请求整体前移一位；默认顺带关流中断） |
| `verify <工程> [--json]` | **让 CubeMX 真跑一次加载-保存**，看它认不认这份工程。只读，不起进程写盘 |
| `config [--set-db D \| --set-cube-mx D] [--json]` | CubeMX 装在哪：不带参数是诊断视图，带 `--set-*` 是存路径。不碰工程 |

写命令都带 `--dry-run` / `--force` / `--no-backup`，中断与 DMA 那四个还带 `--verify`
（落盘前先让 CubeMX 验一遍，见下）。全局还有 `--no-input`（见下）。

**意图级优先。** `add-peripheral` 一次产出自洽配置；`set-pin` 逐字段改，中途必然经过
"还没配完"的中间状态（配了 TX 还没配 RX）。微调、或数据库覆盖不到的冷门外设才用逐字段。

### 中断与 DMA：名字从哪来

`enable-irq` / `add-dma` 这类命令**只收一个名字**（`USART2_IRQn` / `USART2_RX`），
不收外设名 —— 名字在这颗 MCU 上唯一，而且 `DMA2_Stream2_IRQn` 并不"属于" USART1。
名字**直接从 `list` 的输出里抄**，别凭印象拼：

```bash
cubemx-cli list 工程.ioc USART2 --page nvic   # → USART2_IRQn ... 现在是关的
cubemx-cli list 工程.ioc USART2 --page dma    # → USART2_RX / USART2_TX，各能挂哪些通道
```

名字**大小写敏感，原样抄**。拼错了会**列出候选**（先按前缀给一批，再给编辑距离最近的），
照着抄一个回来即可。

### `--verify`：写不常见的东西之前跑一次

`--verify` 把改动**先写到临时文件**，启动一次 CubeMX 做"加载 → 另存"，比对
**"我刚写进去的键，CubeMX 还给我了吗"**，确认了**才**覆盖真文件。它要起 JVM，
**几十秒**，所以不是每次写入都要加 —— 拿不准 CubeMX 认不认某个写法时加一次：

```bash
cubemx-cli add-dma 工程.ioc USART1_RX --verify
```

**没有 CubeMX（或没配 `ST_CUBEMX_EXE`）时 `--verify` 直接报错退出码 1，不会假装验过。**
这和 `validate` 的 `skipped` 是同一条纪律："没验"和"验过了"必须说清是哪一个。
只读体检用独立的 `cubemx-cli verify 工程.ioc`（不改文件）。

### 拿下 `--virtual-mode` 的值

外设级"模式"键的名字不止一个：USART/UART/I2S/SAI/USB 叫 `VirtualMode`，**SPI 叫
`VirtualType`**。别猜 —— 从 `modes --json` 里拿：

```json
{"name": "Asynchronous", "virtual": "VM_ASYNC", "virtual_key": "VirtualMode",
 "signals": ["RX", "TX"]}
```

`virtual` 是**能直接喂回去的值**（`--virtual-mode VM_ASYNC`）。别把 `modes` 的**文本**
输出里的 `VirtualMode=VM_ASYNC` 抄进参数 —— 那是给人读的显示串，抄过去只会得到
"参数不认识"。`virtual_key` 告诉你该用 `--virtual-mode` 还是 `--virtual-type`。

## 核心流程

```
1. 读     list <工程> [<外设>] --json              ← 先弄清现在是什么样、能配什么
2. 查     modes <工程> <外设> --json               ← 合法模式与信号名（要细节时）
3. 试     add-peripheral ... --dry-run            ← 看改动清单 + diff
4. 核     对着 diff 确认（引脚对不对、有没有动到别的外设）
5. 写     同一条命令去掉 --dry-run                  ← 拿不准就加 --verify
6. 验     validate <工程>                          ← 必须通过
```

第 3 步顺手加 `--verify`（`--dry-run --verify`）是个有用的组合：它**在你决定写不写之前**
回答"这次改动 CubeMX 会不会吃掉"，而且答案会进退出码 —— 不看报告也能判断能不能写。
代价是十几秒。

**第 1 步先看全貌，别急着写。** `list <工程>` 给出全部外设和哪些已经配了；
`list <工程> <外设>` 展开它的五页（模式 / 参数 / 中断 / DMA / 引脚），
**没设过的参数也在里面**并带上库里默认值 —— 所以"这个外设还能调什么"是**看**出来的，
不用靠试错撞报错。`--page nvic|dma|gpio` 只看一页。
它完全只读，一个字节都不写盘。

第 3 步失败很正常，而且**报错信息是给你用的**：

```
错误：USART2 没有模式 'Nonsense'。可选：Asynchronous, Synchronous, ...（全列出来）
错误：PA9 无法承载 USART2_TX。可以用的脚：PA2, PD5          ← 直接告诉你该用哪个
```

照着改重试即可，不用去翻数据库。

## 退出码与失败语义

| 码 | 含义 | 你该干什么 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 用法错误（工程不存在、外设名/模式名不存在、引脚被占、引脚扛不住这个信号、**缺参数**、路径不在白名单） | 改命令重试 |
| 2 | **校验没通过**（含 `--verify` 往返没通过）—— 只有这一种情况 | 读 stderr 的报告改配置/改写法 |

看到 2 就去读校验报告；看到 1 就去改你敲的命令。这个分工是刻意的：argparse 自带的用法错误
本来也退 2，会让"少打一个参数"看起来像"工程坏了"。

`--no-input` 可以在任何位置关掉交互式提问（`--no-input info x.ioc` 和 `info x.ioc --no-input`
都认）。**不加也不会卡住**：stdin 不是终端时它自动跳过提问。所有提示语都走 stderr，`--json`
的 stdout 永远是干净载荷。

**校验没过、或 `--verify` 没通过时，磁盘上的文件一个字节都没变，也不会留下备份。**
所以第 3～6 步之间失败可以放心重试，不需要"先恢复再重来"。

两类 error 要分清（`validate` 的输出里会写明）：

- **配错了**（`mode.unknown` / `pin.unsupported_signal` / `virtual.missing` …）→ 挡住落盘，
  退出码 2。必须修。
- **还没配完**（`mode.missing_signals` / `mode.missing`）→ **允许落盘**，退出码 0，但会打到
  stderr 提醒。逐字段编辑天然要路过这些中间状态。配完后再跑一次 `validate` 复核。

所以**一条命令退出码 0 不等于工程是好的** —— 收尾那次 `validate` 才算数。

### 别只看退出码：`validate --json` 的 `skipped`

没有数据库时，`validate` 会返回 **`ok: true` 且退出码 0**，但三层校验**一层都没跑**：

```json
{"ok": true, "issues": [],
 "skipped": ["引脚能力（缺 CubeMX 数据库）", "模式自洽（缺 CubeMX 数据库）", "参数名与取值（缺 CubeMX 数据库）"]}
```

只看 `ok` 会得出"工程没问题"的错误结论。**`skipped` 非空就等于"这些没查"** —— 这时要
说"校验被跳过，无法确认"，不能说"校验通过"。最省事的修法是先自己配好数据库再重跑：

```bash
cubemx-cli config --set-db "D:\stm32CubeMX"
cubemx-cli validate 工程.ioc --json     # 重跑，确认 skipped 空了
```

（也可以让用户设 `ST_CUBEMX_DB=<STM32CubeMX>\db\mcu`，但那样只管他这一次。）

## 详细参考

| 要做什么 | 先读 |
|---|---|
| 完整流程 SOP、逐字段分步配置、常见任务的命令范例 | [workflow.md](references/workflow.md) |
| 命令失败/校验不通过/没数据库，怎么诊断与恢复 | [troubleshooting.md](references/troubleshooting.md) |
| `.ioc` 的坑：登记表、引脚排序、虚拟引脚、`Mode` 的写法 | [ioc-facts.md](references/ioc-facts.md) |
| 校验分五层各查什么、为什么这么设计、已知边界 | [design.md](references/design.md) |

## 交付与说明

改动说清三件事：**动了哪些键**、**diff 里的引脚分配**、**最后 `validate` 的结果**。
没有跑过 `validate` 就不能说"配好了"。若 `validate` 报的是"还没配完"，明确说明**还差什么**
（比如"USART2 缺 RX"），不要说成成功；若三层校验因为没数据库被跳过，也必须说出来 ——
"跳过"和"通过"是两回事。

同样地，**`--verify` 没跑成就不能说"CubeMX 验过了"**。它有三种结果，说的时候要分清：
往返通过（可以写"CubeMX 认这份配置"）、往返没过（**别落盘**，去读丢的是哪个键）、
没法跑（没装 CubeMX / 超时 —— 只说"没验"，不许含糊成"应该没问题"）。
