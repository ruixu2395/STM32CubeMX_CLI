# `.ioc` 的坑

这些都是**实测**出来的 CubeMX 行为，从代码或文档推不出来。猜错不会报错，只会静默出错 ——
这也是"必须走 cubemx-cli、不要手改"的根本原因。

---

## 1. 两张登记表（最关键）

`.ioc` 里每个键必须**登记**进对应的表，否则 CubeMX 加载时**静默丢弃**：

```
USART2.IPParameters=VirtualMode,BaudRate          ← 外设参数
PC13-ANTI_TAMP.GPIOParameters=GPIO_Label,GPIO_PuPd ← 引脚参数
```

实测：手工写 `PB0.GPIO_Label=LED` 但不写 `PB0.GPIOParameters=GPIO_Label`，
CubeMX `config load` → `saveas` 之后那一行就没了。`GPIO_PuPd` / `GPIO_Speed` / `Mode`
同样被丢。登记上就全都保住。

- `Signal` / `Locked` **不需要**登记（CubeMX 自己会把这两个补进表里）。
- 表里只写 `GPIO_*` / 参数名这类，**不写** `Signal` / `Locked` / `Mode`。

工具会自动维护这两张表。手改不会。

## 2. 只有 CubeMX 自己写出的文件才逐字节往返

手工构造的键（尤其没登记的 GPIO 参数）会被 CubeMX 规范化掉。所以**不能拿"往返逐字节
一致"当校验手段** —— 校验必须自己做完，这就是 `validate` 存在的理由。

半配置的工程（引脚有 `Signal`、外设不在 `Mcu.IP*`、`Mode` 缺失）CubeMX 会**静默接受**。

**但有一件事往返能查，而且只有它查得出**：`cubemx-cli verify`（以及写命令上的
`--verify`）问的是另一个问题，比"逐字节一致"窄得多、也可靠得多 ——

> **"我刚写进去的那个键，CubeMX 还给我了吗？"**

判据只有一条：`lost_ours` 非空就不合格。CubeMX 补的默认值、改的计算键、
重排的 `functionlistsort`、`saveas` 改掉的 `ProjectManager.ProjectName` 都只作提示。
这条窄判据经得起实测：一份干净的 `Usb_uart.ioc` 往返下来，**除了那两个
`ProjectManager.*`，一个键都不动**。

它抓的正是 `validate` 抓不到的那类**静默损坏** —— 见 §9 里"孤儿外设的中断会被
CubeMX 悄悄删掉"那条。

## 3. 虚拟引脚的 `Mode` 必须写，而且写的不是外设模式名

虚拟引脚（不占真实引脚，如 `VP_SYS_VS_Systick` / `VP_TIM3_VS_ClockSourceINT`）三变体实测：

| 写法 | CubeMX 的行为 |
|---|---|
| 漏写 `Mode` | **外设从 `Mcu.IP*` 里消失** —— 引脚还在清单里，生成的代码却不初始化它（静默损坏） |
| 写 `Enable_Timer`（外设模式名） | 被改写成 `Internal` |
| 写 `Internal` | 原样保留 ✅ |

该写哪个：查模式库里**声明了这个 `VS_` 信号**的那个 `<Mode>`，再挑**带 `UserName` 属性**
的（`Internal` 有 `UserName="Internal Clock"`；`Enable_Timer` 没有，是无 `UserName` 的
GUI 里点不到的内部伪模式）。

**三者名字常常互不相同**：TIM3 的外设模式是 `Enable_Timer`，它的虚拟引脚却要写 `Internal`。
`add_peripheral` 已经处理了这件事，手写极容易错。

## 4. 排序不是加入顺序

- `Mcu.Pin0..N` 按**封装 `Position` 排序**（即引脚号顺序），不是加入顺序、不是字母序。
  无位号的虚拟引脚排最后。
- `Mcu.IP0..N` 按**字母序**。

所以 diff 里看到 `Mcu.Pin*` 整段重排是**正常现象**，不代表工具乱动。核对时看的是引脚
集合对不对，不是行号。

## 5. 引脚名可能带后缀

`PA0-WKUP`、`PC13-ANTI_TAMP`、`PH0-OSC_IN` 等（F407 有 6 个）。只认裸 `PA9` 的写法会
直接漏掉这些脚。工具的 CLI 两种写法都接受（给 `PC13` 会解析到 `PC13-ANTI_TAMP`），但
`pins --json` 报出来的是全名。

## 6. 外设级"模式"键有两个名字

- USART / UART / I2S / SAI / USB → `VirtualMode`
- **SPI → `VirtualType`**

它和引脚的 `Mode` 是**独立的两个键**，必须一起改。用错名字的键会被静默丢弃。
`modes --json` 的 `virtual_key` 字段会告诉你该用哪个。

## 7. GPIO 引脚只写 `Signal` + `Locked`，不写 `Mode`

ST 官方板级文件里全是这样。GPIO 脚上写 `Mode` 会被接受但多余。非 GPIO 引脚才写 `Mode`，
值来自模式库的 `<RefMode Name>`。

GPIO 的 signal 写法：`GPIO_Input` / `GPIO_Output` / `GPIO_Analog` 直写；但 **EXTI 是
`GPXTI<脚号>`**（如 `PB0` → `GPXTI0`）—— 这个字符串在 MCU 库里查不到，是 CubeMX 合成的，
合法性只能靠"该脚 `IOModes` 含 `EXTI` 且线号 == 脚号"来判断。

## 8. 其他

- 文件格式：扁平 `key=value`、CRLF、UTF-8 无 BOM、键按序数排序、`:` 转义成 `\:`。
  首行固定是 `#MicroXplorer Configuration settings - do not modify`。
- `config saveext` 会把"保持默认"的参数也写出来（`config save` 不写）。这解释了为什么
  CubeMX 往返后外设会多出 `SPI2.Mode=SPI_MODE_MASTER` 之类的默认值。
- **未知的键要保留** —— `.ioc` 是 CubeMX 的私有格式，不是稳定公开的 schema。工具会原样
  保留不认识的键。

## 9. NVIC 与 DMA 的写法（全部实测，猜错会静默出错）

### 9.1 `NVIC.<IRQn>` 的值是变长元组

`NVIC.USART1_IRQn=true\:0\:0\:false\:false\:true\:true\:true\:true`

前 3 段是**语义**（`已启用:抢占:子`），第 4 段往上是 **CubeMX 的版本印记**：

| CubeMX 版本 | 元组长度 |
|---|---|
| 4.13.0 | 4 |
| 4.15 – 4.19 | 6 |
| 5.x | 8 |
| 6.x | 9（6.17 偶有 10） |

同一个 `BusFault_IRQn` 在不同版本的文件里有 6/7/8/9/10 五种长度；6.x 文件里同一个
IRQ 名的尾巴**还会随文件变**（87 个 IRQ 名里 33 个不恒定）。所以那段尾巴
**既推演不出来，也不能照抄别的文件**。工具的做法：

- 键**已存在** → 只改第 0/1/2 段，尾巴**逐字节**原样带走。
- 键**不存在** → 只写最小的 `true:<抢占>:<子>`，**绝不自己编尾巴**。

实测：手写 `NVIC.DMA1_Stream5_IRQn=true\:0\:0` 进去，
CubeMX 加载-保存后自己补成 `true\:0\:0\:false\:false\:true\:true\:true\:true`。
**让 CubeMX 补全**是唯一站得住的写法。

### 9.2 "关中断" = 删掉那一行

手写 `NVIC.X_IRQn=false\:0\:0\:…` 进去，CubeMX 加载后**整行删掉**。写 `false` 是绕
一圈回到同一处，还多一个 CubeMX 自己都不认的中间态。`NVIC.IParameters` 同理 ——
524 份官方板级文件里出现 **0 次**，写了会被静默丢弃。

### 9.3 孤儿外设：中断会被删，DMA 会留成"死块"

给一个**没配置好的外设**（不在 `Mcu.IP*` 里，或占了 `Mcu.IP*` 但一个引脚都没配）
开中断 —— 实测（用例 B）：CubeMX 往返后**那行被悄悄删掉**，不报任何错。
这正是 `--verify` 存在的理由，也是 `validate` 里 `irq.orphan` 那条 warning 的由来。

给它挂 DMA 则相反（用例 C）：`Dma.*` 块**原样留着**，看起来完全正常 ——
但外设没配，那个 DMA 请求永远不会触发。**往返验证抓不到这一种**，只能靠
`dma.orphan` warning。两种都不报错，这是本格式最危险的地方。

### 9.4 `Dma.*` 的形状与索引

- `Dma.RequestsNb` **恒等于** `Dma.Request<n>` 的条数；每个请求**恰好一个槽位**；
  `Dma.<请求>.<n>` 里的 `n` **就是全局请求序号**（9 个请求的 WB 板级文件里
  `Request0..8` ↔ `.0.`..`.8.` 逐个对得上，DMAMUX 型号也一样）。
- `RequestsNb` 在 524 份里取过 1/2/3/6/7/9，**0 一次都没出现过**：没有 DMA 的工程
  压根没有 `Dma.*` 键。所以**删到空时删键，不写 0** ——
  实测（用例 E）：删键之后 CubeMX 也不会把它补回来。
- 删到空时 `Mcu.IP*` 里的 `DMA` 也一并摘掉（实测同样被接受），
  而 CubeMX 自己会把 `ProjectManager.functionlistsort` 里的 `MX_DMA_Init` 一并删掉 ——
  那张表是它按配置**重算**的，**不要手写**。
- 参数清单**照工程里已有的块抄**，不要照库里 `DmaRequest.params`：库里按 `BaseMode`
  展开，比 CubeMX 实际写进文件的多（`Channel` / `DMA_Handle` / `MemBurst` 这些
  CubeMX 从来不写）。照模板抄出来的手写块与 CubeMX 自己写的**逐名一致**（用例 C）。
- `NVIC.ForceEnableDMAVector` **不是 DMA 的标记**（205 份文件带它，其中 193 份一个
  `Dma.*` 都没有），删 DMA 时不要顺手删它。

### 9.5 带 DMAMUX 的型号：库里没有候选通道

`STM32WB5MMGHx` 这类带 DMAMUX 的型号，`db` 里对请求**一个候选通道都不给**
（XML 里连 `DMA*_Stream*` / `DMA*_Channel*` 形态的 `Mode` 都不存在）——
因为请求→通道的映射由 DMAMUX 随时指定，不是固定的。而文件里
`Dma.I2C1_RX.0.Instance=DMA1_Channel1` 照样有值，参数清单也更长
（多出 `RequestNumber` / `SignalID` / `Sync*` / `EventEnable` 等 16 项）。

所以 `add-dma` 在这类型号上**没法自动挑通道**，必须 `--stream` 指名。
报错文案要照实说"库里没给候选"，**不能说成"都被占用了"** ——
那是个没核对过的说法，会让人去查一个不存在的冲突。

## 权威数据源

- `db/mcu/<Mcu.Name>.xml` —— 每个脚合法的 signal。**文件名就是 `.ioc` 里 `Mcu.Name` 的字面值**
  （如 `STM32F407Z(E-G)Tx.xml`，括号不能省）。
- `db/mcu/IP/<IP Name>-<IP Version>_Modes.xml` —— `<RefMode Name>` → 外设级模式键值、
  必需信号（`<SignalLogicalOp>`）、允许的参数。

要查什么，先 `modes --json`；不够再直接读这两个 XML。
