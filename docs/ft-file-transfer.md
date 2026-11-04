# FT（File Transfer）与 FT diagram 概念说明

> 来源：从一次会话中的问答整理，基于本目录代码
> （`auth_test.py`、`ref_iap2_link_layer.py`、`HANDOVER.md`）解释。

## 结论

在本项目的语境里，**FT = File Transfer，即 iAP2 的“文件传输”会话（session 12）**，不是 Fourier Transform 之类的缩写。“FT diagram”多半是指画出来的 **FT 文件传输流程时序图**，用来展示 iPod 和 Linux 配件之间在 FT 会话上怎么来回发消息。

## FT 会话的基本机制

- iAP2 链路建好后（LSP 协商时把 session 12 声明为 type 1, version 2），双方就可以在 session 12 上收发 **FT datagram**。
- 每个 FT datagram 的格式很简单：

```text
[transfer id (1 byte)] [command (1 byte)] [payload...]
```

- `transfer id` 用来区分同时进行的多个传输；`command` 表示消息类型。

## 一次典型传输的流程（FT diagram 一般画的内容）

```text
iPod                                       Linux 配件
    │  Setup  (0x04, 含文件大小8字节+filetype2字节)  │
    │ ─────────────────────────────────────────────▶ │
    │                                        Start  │
    │ ◀────────────────────────────────────── (0x01) │
    │  FirstData/Data/LastData                        │
    │  (0x80 / 0x00 / 0x40)  或 FirstAndOnly (0xC0)   │
    │ ─────────────────────────────────────────────▶ │
    │                                    Success     │
    │ ◀────────────────────────────────── (0x05)     │
    │        （中途可发 Cancel 0x02 中止）            │
```

对应 `auth_test.py` 里的 `make_ft_handler`：收到 Setup → 记下 `size/filetype` 并回复 Start；收到数据分片就累计；收到 LastData/FirstAndOnly 就回 Success；出错回 Failure（0x06）。

命令字节速查：

| 命令 | 值 | 方向/含义 |
|---|---|---|
| Start | 0x01 | 配件 → 设备，确认开始接收 |
| Cancel | 0x02 | 任一方 → 任一方，中止传输 |
| Setup | 0x04 | 设备 → 配件，携带 8 字节 big-endian 文件大小 + 2 字节 filetype |
| Data | 0x00 | 中间数据分片 |
| FirstData | 0x80 | 首个数据分片 |
| LastData | 0x40 | 末尾数据分片 |
| FirstAndOnly | 0xC0 | 唯一一个分片（小文件） |
| Success | 0x05 | 配件 → 设备，传输完成确认 |
| Failure | 0x06 | 配件 → 设备，传输失败 |

## 它在这个项目里用来干什么

- **取封面图**：Now Playing 的媒体项属性 26 是 artwork transfer ID，封面要走 FT 会话（NowPlayingArtworkData，filetype 2）传输，见 `HANDOVER.md` §6.4。
- **媒体库相关文件**：例如播放列表内容、较大的媒体库数据，也可能走 FT（如果设备 LSP 接受 session 12）。

而 Now Playing 本身（`0x5000`/`0x5001`）和 identification（`0x1Dxx`）走的是 control session（session 0）上的 CSM 消息，不经过 FT。

## 相关文件

- `ref_iap2_link_layer.py`：`FT_SESSION_ID = 12`，`file_transfer_handler` 分发入口
- `auth_test.py`：`send_ft()`、`make_ft_handler()` 实现了 FT 接收状态机
- `HANDOVER.md`：§3.2（LSP 中的 session 12）、§6.4（封面走 FT）

## 其他可能的含义

“FT diagram”如果不指本项目的文件传输流程，也可能是：

- **Fault Tree diagram（故障树）**：可靠性/安全分析里的故障树图；
- **Frequency–Time diagram（时频图）**：信号处理里的频谱图（spectrogram）。
