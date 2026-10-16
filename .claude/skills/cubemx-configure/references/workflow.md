# 完整流程 SOP

## 万变不离其宗

```
读现状 → 直接写（每个外设 1～3 条命令） → 全部配完后 validate 一次
```

**以速度优先**（与 SKILL.md 硬规则一致）：不必先 `--dry-run`，不在中途加 `--verify`；
一个键试 2 次不过就跳过、列入未完成项。下文里出现的 `--dry-run` / `--verify` 示例仅在
需要预览抢占/删除操作时参考。

## 第 0 步：数据库就位

没配好数据库的话，后面"查合法选项"和最后的 `validate` 都是空转 —— 三层校验会静默跳过。

```bash
cubemx-cli config --json      # db_root 为 null 就是没配好
cubemx-cli config --set-db "D:\stm32CubeMX"   # 没配好就自己配上（非交互）
```

## 第 1 步：先看全貌（`list`）

不知道"能配什么"的时候，**从这里开始**，不要靠试错撞报错：

```bash
cubemx-cli list proj.ioc --json              # 全部外设 + 哪些已经配了
cubemx-cli list proj.ioc USART1              # 展开五页：模式/参数/中断/DMA/引脚
cubemx-cli list proj.ioc USART1 --page nvic  # 只看中断
```

它**只读**，一个字节都不写盘。两件事值得记住：

* **没设过的参数也在清单里**，并标着库里的默认值 —— 所以"还有哪些能调"是看出来的。
* **中断页包含随 DMA 请求一起出现的 stream 中断**（`USART1` 会带上
  `DMA2_Stream2_IRQn` / `DMA2_Stream7_IRQn`），别以为只有 `USART1_IRQn` 一条。

`list <工程>` 顶层**没有** CubeMX GUI 那棵分类树（`System Core` / `Connectivity` …）——
那些分组标签推不出来，所以按字母序排、只分已配置/未配置。找不到某个外设时直接
搜名字，别去猜它在哪个分类下。

---

## 例 1：加一个串口（意图级，最常见）

用户说"加一个 USART2，波特率 115200"。

```bash
# 1. 先看现状：这颗 MCU 上 USART2 在不在、已经配成什么样了
cubemx-cli list proj.ioc              # 全部外设，已配置/未配置分两段
cubemx-cli list proj.ioc USART2       # 它现在的中断、DMA、引脚（还没配就是空的）
cubemx-cli pins proj.ioc --json       # 哪些脚已经被占了

# 2. 查 USART2 的合法模式与信号名（别猜）
cubemx-cli modes proj.ioc USART2 --json

# 3. dry-run：自动挑引脚
cubemx-cli add-peripheral proj.ioc USART2 --param BaudRate=115200 --dry-run
```

第 3 步的输出：

```
  模式 Asynchronous
    USART2_RX -> PA3
    USART2_TX -> PA2
  PA3.Signal = USART2_RX
  PA3.Mode = Asynchronous
  PA2.Signal = USART2_TX
  PA2.Mode = Asynchronous
  USART2.VirtualMode = VM_ASYNC
  USART2.BaudRate = 115200
--- 修改前
+++ 修改后
@@ -36,8 +36,11 @@
 Mcu.IP3=USART1
-Mcu.IPNb=4
+Mcu.IP4=USART2
+Mcu.IPNb=5
...
+USART2.BaudRate=115200
+USART2.IPParameters=VirtualMode,BaudRate
+USART2.VirtualMode=VM_ASYNC
（--dry-run：没有写入文件）
```

**核对这几件事**：引脚是不是想要的（工具挑的是"能承载该信号且没被占"的第一个，不一定
符合用户的板子）；`Mcu.Pin*` 的**重排**是正常的（按封装位置排序，不是新增顺序）；没有
误动别的外设。

引脚不合适就显式指定：

```bash
cubemx-cli add-peripheral proj.ioc USART2 --tx PD5 --rx PD6 --dry-run
```

信号名用 `modes` 里 `signals` 给的那些（`--tx` / `--rx` 就是 `TX` / `RX` 的小写）。

```bash
# 4. 去掉 --dry-run 真正落盘
cubemx-cli add-peripheral proj.ioc USART2 --param BaudRate=115200

# 5. 复核 —— 这一步不能省
cubemx-cli validate proj.ioc
```

## 例 2：给串口开全局中断 / 挂 DMA

用户说"USART1 收发要用中断"、"USART1 收发都走 DMA" —— 也就是 CubeMX GUI 里
**NVIC Settings** 和 **DMA Settings** 那两页。

### 先看清楚现在是什么状态

```bash
cubemx-cli list proj.ioc USART1 --page nvic
cubemx-cli list proj.ioc USART1 --page dma
```

中断页里每条后面标着现在开着还是关着，**名字（`USART1_IRQn`）直接抄下来当参数**。
DMA 页给出请求名（`USART1_RX` / `USART1_TX`）、每个请求**现在挂在哪**、以及**能挂哪些
通道**（`可选 stream：…` 是数据库给的候选全集，**不排除已被本工程占用的** ——
谁被占用了要看别的请求那几行）。

### 开一条中断

```bash
cubemx-cli enable-irq proj.ioc USART1_IRQn --preemption 5 --sub 0 --dry-run
```

改动清单长这样：

```
NVIC.USART1_IRQn = true:5:0:…（后 6 段原样保留）
```

那"后 6 段"是 CubeMX 自己的账本，**我们不动它**（见 [ioc-facts.md](ioc-facts.md) 的
NVIC 元组一节）。如果这条中断原来没开，清单会是干净的 `NVIC.USART1_IRQn = true:5:0` ——
**三段式，尾巴留给 CubeMX 下次加载时自己补全**（实测它会补）。

不给 `--preemption` 就沿用现值；原来没开就取 0。优先级超出这颗 MCU 的合法区间会被
拒绝，报错里写着区间，照着改。

```bash
cubemx-cli enable-irq proj.ioc USART1_IRQn --preemption 5   # 核对完去掉 --dry-run
```

**关掉它是另一条命令**：

```bash
cubemx-cli disable-irq proj.ioc USART1_IRQn
```

它做的是**删掉那一行**。别手写 `false` —— 文件里"关着的"中断根本没有那一行，
写 `false` 只是让 CubeMX 下次保存时帮你删（中间态还可能被别的工具读错）。

### 挂 DMA

```bash
cubemx-cli add-dma proj.ioc USART2_RX --dry-run
```

不给 `--stream` 就**自动挑第一个没被本工程其它请求占用的通道，并把挑的结果打进清单**
（下面是 STM32F407 上一个真实工程的实测输出）：

```
  USART2_RX → DMA1_Stream5（槽位 2）
  NVIC.DMA1_Stream5_IRQn = true:0:0
```

三条都值得看一眼：

* **通道挑得对不对**。工具按数据库给的候选顺序取第一个空闲的，**它不知道你的板子怎么接**。
  不合适就显式指定：`--stream DMA1_Stream5`（非法通道会报错并列出**全部**合法值）。
* **它顺带开了这个通道的中断**（DMA 传输完成中断，CubeMX 里通常也是这么配的）。
  不想要就 `--no-irq`。
* **工程原来没有 DMA 时，清单里还会多一行 `登记外设 DMA`** —— 不登记进 `Mcu.IP*`，
  CubeMX 不会生成 DMA 初始化代码。工具自动做了，但会说一声。

（顺带一提，实测里 `NVIC.DMA1_Stream5_IRQn` 写的是**三段式** `true:0:0`，
而同一份文件里 CubeMX 自己写的 `NVIC.DMA2_Stream2_IRQn` 有九段 —— 这和"已存在的键
保留尾巴"正好对上：**新建的键我们只写三段，尾巴归 CubeMX 补**。）

DMA 参数（`Mode` / `MemInc` / `PeriphInc` 等）的**值**取库里的默认值，要改用 `--param`；
而**要写哪几个参数名**是照工程里已有的 DMA 块抄的（那是 CubeMX 亲手写的清单，比库里的
准）。工程里一个 DMA 请求都没有时才退回库里的清单 —— 那是唯一没实测过的一支，
第一次在这种工程上挂 DMA，建议加 `--verify`：

```bash
cubemx-cli add-dma proj.ioc USART2_RX --param Mode=DMA_NORMAL --param MemInc=DMA_MINC_ENABLE
```

参数名不在这条请求的合法集合里会被拒绝，并列出**全部**可设的：

```
错误：USART2_RX 没有可设参数 'Nonsense'。可设：FIFOMode, FIFOThreshold, MemBurst,
      MemDataAlignment, MemInc, Mode, PeriphBurst, PeriphDataAlignment, Priority
错误：Mode=DMA_NONSENSE 不在取值范围内，可选：DMA_NORMAL, DMA_CIRCULAR, DMA_PFCTRL
```

想先看看现在是什么、有哪些可设，用 `list proj.ioc USART2 --page dma`。

### 摘掉一个 DMA 请求

```bash
cubemx-cli remove-dma proj.ioc USART2_RX
```

这是唯一会**动到别的请求**的操作：`Dma.Request<n>` 和 `Dma.<请求>.<n>.*` 里的 `n` 是
同一个编号，删掉中间那条，**后面的请求会整体前移一位**。工具会自动重排，清单里会写清
原来在哪个槽位。同时它顺带关掉释放出来的那个通道中断（`--keep-irq` 可保留）。

**如果这是最后一个请求**，清单里会多两行：

```
Dma.RequestsNb 已删除（没有 DMA 请求了）
移除外设 DMA
```

这是对的 —— 文件里"没有 DMA"的真身就是整块 `Dma.*` 都不存在（实测 524 份 ST 官方板级
文件里 `RequestsNb` **一次都没出现过 0**）。和"关中断 = 删行"是同一条规矩。

### 这两类改动建议加一次 `--verify`

中断和 DMA 的键形状是这一轮才实测出来的，风险不在"工具算错了"，而在"**CubeMX 认不认**"。
拿不准就加 `--verify`，它会先写到临时文件、让 CubeMX 真跑一次加载-保存，确认**我们写进去的
键都还在**，才覆盖真文件：

```bash
cubemx-cli add-dma proj.ioc USART2_RX --verify
```

代价是十几秒（要起 JVM）。做完还是那次 `validate` 收尾。

### `--verify` 证明的是"CubeMX 收下了"，不是"配置对"

往返通过只说明**我们写的键还在**。有没有一条路是"键都活着、配置却是死的"？有，
而且两种方向不对称（实测）：

| 给一个**没配好的外设**（占着 `Mcu.IP*`、声明了模式，却一个引脚都没有）… | 结果 |
|---|---|
| **开中断** | CubeMX 往返后那行**被悄悄删掉**，所以 `--verify` 会报"你写的键被丢了"并拒绝落盘 |
| **挂 DMA** | `Dma.*` 块**原样存活**，看起来完全正常 —— 但那个请求永远不会触发 |

也就是说 **DMA 这一种 `--verify` 抓不到**，只有 `validate` 的 `dma.orphan` 警告会告诉你
（`irq.orphan` 同理，两条都是**不挡落盘**的 warning）。所以 `--verify` 通过 ≠ 万事大吉，
收尾那次 `validate` 一次都不能省。

真正的修法都是同一件事：**先把那个外设配好**（`add-peripheral ... --tx/--rx ...`），
再回来开中断、挂 DMA。

## 例 3：点亮一个 LED（GPIO）

```bash
cubemx-cli add-gpio proj.ioc PC13 --mode Output --label LED --set GPIO_PuPd=GPIO_UP
cubemx-cli validate proj.ioc
```

`--mode` 只有四个：`Input` / `Output` / `Analog` / `EXTI`（外部中断）。

输出里引脚名可能和给的不一样 —— `PC13` 存进去是 `PC13-ANTI_TAMP`（封装上的完整脚名）。
**这不影响后续调用**：裸名 `PC13` 和全名 `PC13-ANTI_TAMP` 都被接受，用哪个都行。

## 例 4：微调已有配置（逐字段）

用户说"把 USART1 的波特率改成 9600"：

```bash
cubemx-cli peripherals proj.ioc --json          # 确认 USART1 存在、现在什么参数
cubemx-cli set-peripheral proj.ioc USART1 --param BaudRate=9600 --dry-run
cubemx-cli set-peripheral proj.ioc USART1 --param BaudRate=9600
cubemx-cli validate proj.ioc
```

参数名必须**属于这个外设**。写一个不存在的参数名会失败（`set-peripheral` 会报"参数
越界"类的错），这正是防手滑的设计 —— 别用 `--force` 硬塞。

删参数用 `--unset`：

```bash
cubemx-cli set-peripheral proj.ioc USART1 --unset BaudRate
```

删掉必需键（比如 `VirtualMode`）会落盘但报告 `virtual.missing` —— 那种情况下工程是自相
矛盾的，别就这么交出去。

## 例 5：分步配置（逐字段，数据库覆盖不到时）

有些外设 `add-peripheral` 配不出来（模式库里没有合适模式），或用户要求一步一步来。
逐字段流程是：**每个引脚先配 Signal + Mode，最后配外设级参数**。

```bash
cubemx-cli set-pin proj.ioc PA2 --signal USART2_TX --mode Asynchronous
cubemx-cli set-pin proj.ioc PA3 --signal USART2_RX --mode Asynchronous
cubemx-cli set-peripheral proj.ioc USART2 --virtual-mode VM_ASYNC --param BaudRate=115200
cubemx-cli validate proj.ioc
```

**`--mode` 每次都要给。** 只写 `Signal` 不写 `Mode`，CubeMX 不会把这一脚算进外设初始化
（`validate` 会报 `mode.missing`）—— 这是"文件看着对、代码里却没有"的典型。

中间几步会报 `mode.missing_signals`（USART2 缺 RX）之类的"还没配完"，**退出码是 0，允许
落盘**，这是刻意的。但流程走完必须再跑一次 `validate`，它才是不打折扣的总检查。

`--virtual-mode` 的值从 `modes --json` 的 `virtual` 字段拿；SPI 要改用 `--virtual-type`。

## 例 6：清理

```bash
cubemx-cli clear-pin proj.ioc PB0                      # 清掉某个脚的配置
cubemx-cli remove-peripheral proj.ioc USART2            # 移除外设（连引脚一起清）
cubemx-cli remove-peripheral proj.ioc USART2 --keep-pins # 只摘外设，引脚留着
```

`clear-pin` 一个外设的某个脚会让它变成"还没配完"（比如 USART1 只剩 RX），这是允许的中间态。

## 拿不准的时候

| 情况 | 命令 |
|---|---|
| 这个外设到底能配成什么？ | `modes <工程> <外设> --json` |
| 哪些脚能承载我要的信号？ | 故意 dry-run 一个错的脚，报错会列出**全部**合法脚 |
| 这个外设有哪些中断 / DMA 请求，现在什么状态？ | `list <工程> <外设> --page nvic\|dma` |
| 中断名 / 请求名怎么写？ | 同上，输出里的名字**原样抄**；拼错时报错会列候选 |
| 这条请求能挂哪些通道？ | `list <工程> <外设> --page dma` 的 `可选 stream` |
| 现在到底配了什么？ | `pins --json` / `peripherals --json` |
| 想看原始键值 | `show <工程>`（只读，别用它改） |
| 工程到底行不行？ | `validate <工程>` |
| **CubeMX 认不认这份工程？** | `verify <工程>`（只读，起一次 CubeMX，十几秒） |

## 交出去之前

- 最后一条命令是 `validate`，且它的结果被如实说明。
- `validate` 报"还没配完"时，明确说**还差什么**，不要说成成功。
- 加过 `--verify` 就把它**三种结果**说清：往返通过 / 往返没过（没落盘）/ 没验成
  （没装 CubeMX、超时）。**"没验成"不许说成"应该没问题"。**
- 改动过 `.ioc` 后，用户需要在 CubeMX 里重新打开（或 Generate Code）才会生效 ——
  本工具只改文件，不生成代码。
