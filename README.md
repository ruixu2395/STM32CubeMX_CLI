# cubemx-cli

读写 STM32CubeMX 工程（`.ioc`）的命令行工具，**给 AI Agent 用**：加串口、挂 DMA、开中断、
点灯这类活儿，说一句就能配好，而且配错会当场被拦下。

零依赖（纯标准库），有单文件 exe。

## 它解决什么问题

`.ioc` 是 CubeMX 的私有文本格式（`key=value` 扁平行），**可以**直接编辑 —— 但手改会静默出错：

```
USART2.IPParameters=VirtualMode,BaudRate     ← 这两张"登记表"必须一起维护
PC13.GPIOParameters=GPIO_Label,GPIO_PuPd
```

手写 `PC13.GPIO_Label=KEY` 却漏了登记表，CubeMX 打开时那一行**就没了**，不报任何错。
文件看着对，工程却是空的 —— 这正是 AI 最容易撞上的失败方式。

所以这个工具的存在理由不是"能改文件"，而是**改完能校验**：引脚扛不扛得住这个信号、
模式和信号自不自洽、参数名和取值合不合法，全部去查 CubeMX **自己安装目录下的 XML
数据库**，不是内置映射表（内置表在冷门外设上必然错）。

## 原理

```
.ioc --load--> 内存视图 --(set-pin / add-peripheral / enable-irq …)--> 校验 --> 落盘 .ioc
                                                                        └─ 不通过：原文件一个字节都不变
```

三层，一层比一层贵，按需用：

**1. 直接改文本，没有 JSON 中间态。**
`.ioc` 本身就是纯文本，直接在上面做字段级编辑即可。中间加一层 JSON 只会多一份需要同步的
真相。未触碰的键**字节级不变**，未知键**一律原样保留** —— `.ioc` 是私有格式，不是稳定的
公开 schema，丢键比不认键危险得多。落盘是原子的（临时文件 + rename），写盘前默认留一份
`.bak-<时间戳>` 备份。

**2. 五层校验，依据 CubeMX 的 XML 数据库。**
引脚能力 / 归属登记 / 模式自洽 / 参数合法 / NVIC 与 DMA 状态。error 分两类，因为「配错了」
和「还没配完」是两回事：配错了**挡住落盘**，逐字段编辑天然经过的中间态**放行**（退出码 0，
但会在 stderr 上说明还差什么）。

**3. 往返验证 —— 让 CubeMX 亲自读一遍。**
上面两层查的是「配置准不准」，但 `.ioc` 是私有的，还有一类它查不出：**你写了、CubeMX 不认、
加载时静默丢掉**。所以还有一层：把候选内容写到临时副本上，跑一次
`STM32CubeMX.exe -q`（`config load` → `config saveas` → `exit_mx`），逐键比回来，
问一个问题 —— **「我刚写进去的那个键，CubeMX 还给我了吗？」** 确认了才覆盖原文件。

它不默认开（起一次 JVM 十几秒），是"写不常见的东西之前跑一次"的工具，用 `--verify` 触发。
**没跑成 ≠ 验过了**：没装 CubeMX、超时、没产出结果文件，一律不写盘、退出码 1，并说清是哪一种。

## 用之前要配什么

### 1. 拿到命令

两种拿法：

```bash
# a. 直接用仓库里现成的 exe —— 不用装 Python，拷贝就能跑。它在 skill 根目录下：
.claude/skills/cubemx-configure/cubemx-cli.exe --help

# b. 从源码装（Python 3.11+），装完 PATH 里就有 cubemx-cli
pip install -e ".[dev]"
```

（`dist/` 不入库，是本地打包产物。要自己打见下面「打包成 exe」。）

> **名字有仨，对不上是历史原因，不影响使用。** 发行名/命令 = `cubemx-cli`，Python 的
> import 包 = `cubemx_mcp`，仓库目录叫什么都行 —— 项目最早是奔着 MCP server 做的，
> 那条路砍掉只做 CLI 之后包名没跟着改。发行名以 `pyproject.toml` 里的 `name` 为准。

### 2. 配数据库 —— 这一步不做，工具等于半残

引脚能力、模式、参数这三层校验的依据是 CubeMX 安装目录下的 XML（`db/mcu/`）。
**没配的话，工具会明说跳过了哪些检查，不会假装通过** —— 但"没查"和"查过没问题"是两回事。

```bash
cubemx-cli config --set-db D:\stm32CubeMX     # 给安装目录即可，会自动找到 db\mcu
cubemx-cli config                              # 不带参数 = 诊断视图，看配没配上
```

存进 `%APPDATA%\cubemx-cli\config.json`，以后不用再管。路径按三级解析，先命中先用：

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | `ST_CUBEMX_DB` 环境变量 | 只管这一次；适合临时换库 |
| 2 | 用户配置文件 | `config --set-db` 写的 |
| 3 | 自动探测 | 从 `ST_CUBEMX_EXE` 或常见安装位置找到 `STM32CubeMX.exe`，取旁边的 `db\mcu` |

**设了但指错不会瘫掉**：记一条原因、继续往下找，而且点名叫出是哪个变量的问题：

```
$ ST_CUBEMX_DB=E:\nope cubemx-cli config
数据库    D:\stm32CubeMX\db\mcu
来源      配置文件 —— 配置文件 C:\...\config.json

问题：
  - ST_CUBEMX_DB=E:\nope 不是一个存在的目录，已忽略。
```

没数据库时意图级命令会直接拒绝，逐字段命令仍可用但**没有任何能力校验**。

### 3. （可选）CubeMX 的位置 —— 只有 `--verify` 需要

```bash
cubemx-cli config --set-cube-mx D:\stm32CubeMX     # 记下 STM32CubeMX.exe 的位置
```

常规读写都**不需要**启动 CubeMX，只有显式加 `--verify`（或跑 `verify` 命令）时才起一次。
不配也能用，只是用不了往返验证 —— 那时交付必须如实说"这次没有验过"。

### 4. 安全开关

```bash
ST_CUBEMX_ALLOWED_ROOTS=D:\proj;E:\work    # 路径白名单，防止误操作敏感路径
```

它**故意不存进配置文件** —— 这是安全边界，应该每次由调用方显式给出，不该被一个"配一次就忘"
的文件固化。同理，命令行参数经过结构化校验，**不把调用方传来的任意字符串拼进命令行**。

### 打包成 exe

```bash
pip install -e ".[build]"
pyinstaller packaging/cubemx-cli.spec --noconfirm
# -> dist/cubemx-cli.exe，单文件，约 8 MB，零依赖
```

exe 的入口是 `packaging/entry.py`，比 `python -m` 多做一件事：把控制台和两个流的编码掰成
UTF-8。这不是洁癖 —— 在 `cp437` 这类代码页下打印中文会当场崩。

## 用法

### 命令一览

| 命令 | 用途 |
|---|---|
| `list <工程> [<外设>] [--page mode\|param\|nvic\|dma\|gpio]` | **先看有什么**：外设清单，或某个外设的五页配置 |
| `info` / `pins` / `peripherals` / `modes <工程> <外设>` | MCU 型号封装 / 引脚 / 外设与引脚 / 模式与所需信号 |
| `validate <工程>` | 跑全部校验 |
| `verify <工程>` | 让 CubeMX 亲自读一遍（只读诊断，十几秒） |
| `add-peripheral <工程> <外设> [--mode M] [--param K=V] [--<信号> 引脚]` | **意图级**：自动挑引脚、定模式、写全派生键 |
| `add-gpio <工程> <引脚> --mode Input\|Output\|Analog\|EXTI` | **意图级**：配 GPIO |
| `set-pin` / `set-peripheral` / `clear-pin` / `remove-peripheral` | 逐字段改 |
| `enable-irq` / `disable-irq` | 开/关全局中断 |
| `add-dma` / `remove-dma` | 挂/摘 DMA 请求 |
| `show <工程>` | 原样打印 |
| `config` | CubeMX 装在哪 |

只读命令都支持 `--json`。所有写命令都有 `--dry-run`（只打印改动清单和 diff）、
`--no-backup`、`--force`（有 error 也落盘，**绕过安全网，别随手加**），中断与 DMA 那四个
还带 `--verify`。

### 例子

```bash
# 加一个串口，引脚、模式、派生键自动补齐
cubemx-cli add-peripheral proj.ioc USART2 --param BaudRate=115200

# 不想让它自动挑引脚就显式给；挑不到会整体失败并说明哪些脚可用、被谁占了
cubemx-cli add-peripheral proj.ioc USART2 --tx PA2 --rx PA3

# 点个灯
cubemx-cli add-gpio proj.ioc PC13 --mode Output --label KEY

# 开中断 / 挂 DMA —— 名字直接抄 list 的输出，别凭印象拼
cubemx-cli list        proj.ioc USART1 --page nvic
cubemx-cli enable-irq  proj.ioc USART1_IRQn --preemption 5 --sub 0
cubemx-cli add-dma     proj.ioc USART1_RX --stream DMA2_Stream2

# 改一个字段
cubemx-cli set-peripheral proj.ioc USART1 --param BaudRate=9600

# 收尾 —— 这一步不能省
cubemx-cli validate proj.ioc
```

推荐流程是固定的六步，**最容易跳的是 `--dry-run` 和最后那次 `validate`**：

```
读现状 → 查合法选项 → --dry-run 看 diff → 核对 → 落盘 → validate 复核
```

`list` 是只读的浏览入口（一个字节都不写盘），**没设过的参数也照样列出来并带上库里默认值** ——
所以"这个外设还能调什么"是**看**出来的，不用靠试错撞报错。报错信息也是给你用的：

```
错误：USART2 没有模式 'Nonsense'。可选：Asynchronous, Synchronous, ...（全列出来）
错误：PA9 无法承载 USART2_TX。可以用的脚：PA2, PD5          ← 直接告诉你该用哪个
```

### 退出码

| 码 | 含义 | 该怎么办 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 用法错误（缺参数、路径不存在、外设名不存在、白名单挡下），以及**往返验证没跑成** | 改命令 / 修环境后重试 |
| 2 | **工程校验没过**（含 `--verify` 往返没过） | 读 stderr 上的报告，改配置 |

**2 只留给「校验没过」这一种情况。** 这是刻意的：argparse 自带的用法错误本来就退 2，
和它撞了，统一归到 1 —— 否则 Agent 会把"少打了一个参数"读成"这个工程有问题"，然后去修
一个根本没坏的东西。"没装 CubeMX 所以没验成"同理归 1：那是环境问题，不是这份工程有问题。

**校验没过、或往返没过时，磁盘上的文件一个字节都没变，也不会留下备份** —— 可以放心重试。

非交互场景（脚本、Agent）可以加 `--no-input` 关掉一切提问。**不加也不会卡住**：stdin 不是
终端时自动跳过提问。所有提示语都走 stderr，`--json` 的 stdout 永远是干净载荷。

## 给 AI Agent 用

`.claude/skills/cubemx-configure/` 是一份 Claude Code Skill，把用法、流程和 `.ioc` 的坑
编成了可加载的知识：

```
.claude/skills/cubemx-configure/
├─ SKILL.md                 # 入口：命令怎么选、核心流程、四条硬规则
├─ cubemx-cli.exe           # 打包好的单文件工具（PATH 里没有 cubemx-cli 时直接用这个）
└─ references/
   ├─ workflow.md           # 完整 SOP + 常见任务的命令范例
   ├─ troubleshooting.md    # 失败诊断与恢复
   ├─ ioc-facts.md          # .ioc 的坑（登记表、引脚排序、虚拟引脚、VirtualMode vs VirtualType）
   └─ design.md             # 校验分五层、几条设计取舍、已知边界
```

装上就能用：把 `.claude/skills/` 放进工程或 `~/.claude/skills/`，然后直接说"给这个工程加个
USART2，波特率 115200"，Agent 会自己走那六步。

写操作有四条硬规则（写在 SKILL.md 里）：**不要用 Edit/Write 手改 `.ioc`**（登记表不会维护，
键会被静默丢弃）、**写之前先 `--dry-run`**、**关中断 = 删掉那一行**而不是写 `false`、
**不要用 `--force` 绕过校验**（报错就是配错了）。

> 最后提醒一句：本工具**只改 `.ioc` 文件，不生成代码**。改完要在 CubeMX 里重新打开工程
> （或 Generate Code）才会体现到代码里。

## 深入

「为什么这么设计」的东西 —— 五层校验各查什么、NVIC 元组为什么只写三段、
为什么不照抄 CubeMX 的 GUI 分类树、哪些边界是量出来的 —— 见
[`references/design.md`](.claude/skills/cubemx-configure/references/design.md)。
