# 校验层次、设计取舍与已知边界

这份是从 README 里搬出来的**实现细节**。README 只讲「是什么、怎么用、先配什么」，
讲「为什么是这样」的东西放这儿 —— 里面的数字大多是拿 CubeMX 自带的 XML 和 524 份
ST 官方板级工程**量**出来的，不是推断。

## 校验分五层

| 层 | 查什么 | 依据 |
|---|---|---|
| 1 | 引脚存在吗？这个脚扛得住这个 signal 吗？ | `db/mcu/<MCU>.xml` |
| 2 | 引脚所属的外设在 `Mcu.IP*` 里吗？参数登记进 `IPParameters` 了吗？ | `.ioc` 自身 |
| 3 | `Mode` 与 signal 自洽吗？`VirtualMode` 和 `Mode` 对得上吗？缺信号吗？ | `db/mcu/IP/*_Modes.xml` |
| 4 | 参数名属于这个模式吗？取值在枚举范围内吗？ | `db/mcu/IP/*_Modes.xml` |
| 5 | 中断/DMA 挂了没配好的外设吗？写了 CubeMX 会丢的键吗？ | `.ioc` + 数据库 |

第 2 和第 5 层不需要数据库，所以即使没装 CubeMX 也照常跑。

**`ISSUE` 分两级：error 和 warning。** 但 error 里又分两类，因为「配错了」和
「还没配完」是两回事：

- `mode.unknown` / `param.unregistered` 这类 —— **配错了**，挡落盘。
- `mode.missing` / `mode.missing_signals` / `virtual.missing` 这类 —— **还没配完**，
  不挡。否则逐字段流程连第一步都走不完（先 `set-pin --signal` 的话，
  `Mode` 还没写，此时工程必然是不完整的）。

配完后跑一次 `validate` 复核即可。

第 5 层两条都只是 **warning，都不挡落盘** —— 它们描述的是「现在还没坏、CubeMX 一保存
就坏」，属于"提醒，不拦"：

- `nvic.bogus_key` —— `NVIC.IParameters`。别的键名空间才有登记表
  （`<外设>.IPParameters` / `<引脚>.GPIOParameters`），NVIC 没有，写了会被静默丢弃。
  实测 524 份 ST 官方板级文件里出现 **0 次**。只精确点名这一个键，**不做 `NVIC.*`
  通用白名单** —— 那个键名空间是私有的，通用白名单一定会在没见过的 MCU 上误报。
- `irq.orphan` / `dma.orphan` —— 给**声明了模式却一个引脚都没占**的外设开中断或挂
  DMA。前者 CubeMX 会连外设一起删掉，后者更阴：`Dma.*` 块**原样留着**、看起来完全
  正常，而那个请求永远不会触发 —— 所以往返验证也抓不到后一种，只有这条静态规则能说。

**`irq.orphan` 的判据是拿 524 份官方文件量出来的**，因为一条假的 warning 会让人去查
一个不存在的故障。第一版按「登记了但没引脚」判，扫出来 5 份命中：`DFSDM1`（它的引脚
信号叫 `S_DATAIN5DFSDM1`，实例名是**后缀**）、`ADC1`（走内部通道参数，不是引脚）、
`TIM1`（走强制输出）—— **没有一份是真的**。加上「必须声明了 `VirtualMode`/`VirtualType`」
这个门槛后 **512/512 全部干净**，而真会出事的那个形态照样报。

模式键在这里是**判据**而不只是巧合：是模式在要求引脚。ADC 走内部通道、DFSDM 走滤波器
参数都是合法的无引脚配置。复现方式：拿 CubeMX 自带的
`db/plugins/boardmanager/boards/*.ioc` 全扫一遍即可。

## 几条刻意的设计取舍

**不猜，去查数据库。** 引脚能力、模式所需信号、参数取值全部来自 CubeMX 自己的
XML。内置映射表会在冷门外设上出错，而这正是 AI 最需要工具可靠的地方。

**不猜 `Mode`。** 引脚上的 `Mode`（`Asynchronous`）和外设上的 `VirtualMode`
（`VM_ASYNC`）**不是一一对应**，所以内置映射表必然是错的。以 USART 为例：

| | |
|---|---|
| 有信号需求的模式 | 10 个 |
| 它们用到的 `VirtualMode` 取值 | **4 个**（`VM_ASYNC` / `VM_SYNC` / `VM_IRDA` / `VM_SMARTCARD`） |
| 完全没有 `VirtualMode` 的模式 | 3 个（`CTS_Only` / `RTS_Only` / `CTS_RTS`） |

多对一：`Asynchronous` / `Half_duplex` / `Multiprocessor` / `LIN` 全都映射到
`VM_ASYNC`。所以工具是从模式库里**查**，不是从名字**推**。

**虚拟引脚的 `Mode` 有坑，已处理。** `VP_TIM3_VS_ClockSourceINT` 这类虚拟引脚
**必须写 `Mode`，且写的不是外设模式名**。实测（三变体 CubeMX 往返）：

| 写法 | `config load` → `saveas` 之后 |
|---|---|
| 不写 `Mode` | **TIM3 从 `Mcu.IP*` 里消失** —— 引脚还在清单里，外设却没了，生成的代码不会初始化它 |
| `Mode=Enable_Timer` | TIM3 保住，但被 CubeMX 改写成 `Internal` |
| `Mode=Internal` | TIM3 保住，原样保留 |

区分依据是模式库里的 `UserName`：`Internal` 有 `UserName="Internal Clock"`（GUI 里真能
点的一项），`Enable_Timer` 没有（内部伪模式）。`ModeDb.virtual_pin_mode()` 按这条规则
选，取值经过 525 份 ST 官方板级 `.ioc` 交叉验证。

**NVIC 的尾巴只带不改，新建的绝不自己编。** `NVIC.<IRQn>` 的值是**变长**元组
（实测 3～10 段），而长度**是 CubeMX 的版本印记、不是语义**：

| CubeMX 版本 | 段数 |
|---|---|
| 4.13.0 | 4 |
| 4.15 – 4.19 | 6 |
| 5.x | 8 |
| 6.x | 9（6.17 偶见 10） |

同一个 `BusFault_IRQn` 在库里的 524 份官方板级文件中出现过 6/7/8/9/10 **五种**长度；
6.x 的 87 个 IRQ 名里有 **33 个**同一个名字在不同文件里尾巴不同 —— 所以它既推演不出来、
也**不能照抄别的文件**（连"照抄同文件的兄弟"都是错的）。于是：

| 情形 | 工具怎么做 | 实测依据 |
|---|---|---|
| 键已存在 | 只改前 3 段，尾巴逐字节带回去 | — |
| 键不存在 | 只写 `true:<抢占>:<子>` 三段 | 写 `true:7:0` 进去，CubeMX 加载后自己补成 9 段、7 保住 |
| "关闭" | **删掉那一行** | 写 `false:0:0:…` 进去，CubeMX 加载后整行消失 |

让 CubeMX 自己补全是唯一站得住的写法。同理，`add-dma` 的参数清单是**照工程里已有的
DMA 块抄**的（那是 CubeMX 亲手写的），只有工程里一个请求都没有时才退回库里的清单 ——
那一支是唯一没实测过的，加 `--verify` 就有答案。

**没有 CubeMX 那棵分类树，故意的。** GUI 顶层的 `System Core` / `Connectivity` /
`Timers` 这些**分组标签推不出来**，所以 `list` 顶层按字母序排，只分「已配置 / 未配置」。
证据：

| 依据 | 结果 |
|---|---|
| 1395 份 `*_Modes.xml` 里的 `IpGroup` | 719 份根本没有、203 份是空串 |
| 有值的那些（`Connectivity` / `System` / …） | 与 GUI 对不上（`IWDG` 标 `System`，GUI 在 `System Core`） |
| GUI 的标签字符串 | 在 244 MB 的 jar 里一个都搜不到 |

手搓一张分组表能凑出"看起来对"的树，但它不是推演来的，会随 MCU 型号漂移，而且
**错得静默** —— 这正是本工具处处避免的。

**默认值取"不带条件"的那条声明。** 同一个参数常被声明多次、各带一个 `<Condition>`
（同一个 `WordLength` 在 `VM_ASYNC` 和 `VM_IRDA` 下取值不同）。条件表达式是 CubeMX
私有语法，本工具**不求值**，取值合法性按**并集**算（宁可漏报也不误杀）；但**默认值**
只认不带条件的那条：

| 参数 | 第一条声明 | 真默认值 |
|---|---|---|
| `Parity` | `PARITY_EVEN`（条件 `VirtualMode = VM_SMARTCARD`） | **`PARITY_NONE`** |
| `HwFlowCtl` | `UART_HWCONTROL_RTS`（条件 `$IpInstance_R`） | **`UART_HWCONTROL_NONE`** |

按"先见先得"合并就能报出这两个假默认值，让人以为"Parity 默认是偶校验"。

**`NVIC.<名>` 里只有带 `IRQn` 后缀的才算中断。** 其余是普通标量，实测 524 份 ST
官方板级 `.ioc` 里出现过 8 种（`PriorityGroup` / `ForceEnableDMAVector` / `TimeBase` /
`TimeBaseIP` / `BFHFNMINS` / `PRIS` / `Saved{X}IrqHandlerGenerated`）。用后缀而不是
白名单，是因为白名单会漂移 —— 而数据库里 69 份 NVIC 文件的 **5141 条中断全部**
以 `IRQn` 结尾，后缀这条规则是整个库验证过的。

**明文存值，转义只在边界做。** `.ioc` 里 `:` 写作 `\:`（真实工程里有 97 处）。
内部视图里是明文 `Plot: All Steps`，写盘时才转义。

**未触碰的键字节级不变。** 解析存的是原始转义字符串，只有改过的字段才重新渲染。
对转义规则理解有偏差也不会破坏文件。未知键一律原样保留。

**`.ioc` 的键严格按 ordinal 排序**（注意是字符串序：`PA10` 排在 `PA9` 前），
`Mcu.Pin*` 按封装位号、`Mcu.IP*` 按字母序。写盘时全部重排，与 CubeMX 一致。

## 现状与后续

已完成：

- `.ioc` 读写的字节级往返、引脚与外设的字段级配置、意图级 `add-*`
- 五层校验（引脚能力 / 归属登记 / 模式自洽 / 参数合法 / NVIC 与 DMA），依据 CubeMX 数据库
- **只读浏览 `list`**：外设清单 + 模式/参数/中断/DMA/引脚的配置页，全部从数据库推演
- **中断与 DMA 的写入命令**：`enable-irq` / `disable-irq` / `add-dma` / `remove-dma`
- **CubeMX 往返验证**：`--verify`（落盘前）与 `verify`（只读诊断），带 240 秒超时与进程树清理
- CLI，含 `--dry-run` / 备份 / 原子落盘 / `--json`
- 数据库位置的三级解析（环境变量 → 用户配置 → 自动探测）+ `config` 诊断命令
- 打包成**单文件 exe**（PyInstaller，无数据文件、无第三方依赖）
- Skill 层（`.claude/skills/cubemx-configure/`）
- 对真实 CubeMX 做过往返验证：写进去的每一个键都存活，`Mcu.Pin*` 顺序与 CubeMX 自己排的完全一致

已知边界（都是量出来的，不是猜的）：

- **`add-dma` 在带 DMAMUX 的型号上没法自动挑通道。** `STM32WB5MMGHx` 这类型号的数据库里
  对一个请求**一个候选通道都不给**（XML 里连 `DMA*_Channel*` / `DMA*_Stream*` 形态的
  `Mode` 都不存在），因为请求→通道的映射由 DMAMUX 随时指定。这时必须 `--stream` 指名，
  而指名也过不了 `stream in req.streams` 那关（候选是空的）—— 报错文案会照实说"库里没给
  候选"，**不会说成"都被占用了"**（那是个没核对过的说法）。真要支持得单独实测一轮
  DMAMUX 那 16 项参数（`SignalID` / `RequestNumber` / `Sync*` 都是请求相关的），
  写错会造出一个"看起来对"的块，暂时不做。
- **`irq.orphan` / `dma.orphan` 对"信号名不带实例名前缀"的外设会漏报。**
  `HDMI_CEC` 的引脚信号叫裸 `CEC`、`DFSDM1` 的叫 `S_DATAIN5DFSDM1`（实例名在后缀），
  而判"这个外设有没有引脚"走的是 `signal_owner` 的前缀匹配。这类外设一律**不说** ——
  宁可漏报，不可在 512 份官方文件上误报。
- **12/524 份 ST 官方板级文件读不进来**（读入侧的既有限制）：10 份在第 2 行有一条 `#`
  时间戳注释、1 份有重复键（`Mcu.ContextProject`）、1 份第 1 行是空行。`ioc/raw.py` 要求
  header 之后每一行都是 `key=value`。这是**已有**的行为，不是某一轮引入的。
- 新增外设后不会自动补 `NVIC.<外设>_IRQn` 等派生键 —— 现在有 `enable-irq` 了，但得自己
  点名。本工具只保证「意图键」正确，其余派生状态交给 CubeMX 自己补齐。
- **时钟树（`RCC.*`）只有裸写、没有校验。** `set-peripheral 工程.ioc RCC --param PLLN=336`
  能写进去（也会正确追加到 `RCC.IPParameters`），但 `PLLN=999` / `PLLN=abc` /
  `PLLP=RCC_PLLP_DIV9` 一样照收，`validate` 甚至返回"校验通过"。根因是 `validate.py` 里
  把 RCC 和 NVIC 一起放进了「不走 `IPParameters` 登记表」的豁免名单 —— 那条对 NVIC 成立
  （实测 524 份文件里 `NVIC.IParameters` 出现 0 次），**对 RCC 不成立**（`RCC.IParameters`
  524/524 全都有）。
- 它**只改文件、不生成代码**，改完要在 CubeMX 里重新打开或 Generate Code 才生效。

明确**不做**：

- **MCP server。** CLI 本身已经是一个完整的 Agent 接口：`--dry-run` 给了预览，diff 给了
  复核依据，退出码和 stderr 给了纠正重试所需的全部信息，`--json` 给了结构化数据。再包一层
  MCP 不会带来新能力，却要多维护一条代码路径、每个客户端各配一次，还要让工具定义常驻上下文。
  Skill 管「连上之后怎么做」，而这里「连上」这一步 CLI + Bash 就够了。
- **暴露「任意原始 CubeMX 命令」。** 只提供语义化命令，参数经过结构化校验 —— 不把调用方
  传来的字符串拼进命令行。
- **`--all` 批量开关一个外设的全部中断。** 一次一个名字，和 `list --page nvic` 的输出
  一一对应，AI 抄得准。
