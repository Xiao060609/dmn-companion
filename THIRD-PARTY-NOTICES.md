# 第三方组件与许可证

本仓库（dmn-companion）**不包含**下列项目的源码或模型权重。
它们由使用者自行获取和安装，本项目只通过公开接口调用它们。

> 这个文件存在的原因：这些项目有自己的许可证，其中 AstrBot 是 AGPL-3.0
> （强 copyleft）。虽然本项目与它们只是「进程外调用」关系、不构成衍生作品，
> 但用到了就应当写清楚，让来的人知道自己在装什么、受什么约束。

---

## 1. AstrBot

- **项目**：<https://github.com/AstrBotDevs/AstrBot>
- **用途**：QQ 接入的机器人框架。本项目通过 HTTP 调用它，并在它上面挂了
  本项目的定制插件（`qq/` 下只有这些定制部分，**不含 AstrBot 源码**）。
- **许可证**：**AGPL-3.0**

### 这意味着什么

- AstrBot 本身受 AGPL-3.0 约束，使用 AstrBot 提供服务时，
  **对 AstrBot 本身的修改需要开源**。本仓库没有修改它。
- 本项目的定制插件通过 AstrBot 的插件机制加载，属于独立进程外的调用关系，
  不是 AstrBot 的衍生作品，因此本项目整体仍以 MIT 发布。
- **如果你 fork 本项目并修改了 AstrBot 本体**，那部分修改要遵守 AGPL-3.0。

---

## 2. Breeze-TTS-2.cpp

- **项目**：<https://github.com/HoppouAI/Breeze-TTS-2.cpp>
- **用途**：本地语音合成引擎。本项目通过子进程 + HTTP 调用它
  （见 `desktop/tts_breeze.py`，那是本项目自己写的调用层，
  **不含引擎源码**）。
- **许可证**：分三块，需要分开看

| 组成部分 | 许可证 |
|---|---|
| 源码 | Apache-2.0 |
| MP3 编码器（`third_party/shine`） | LGPL-2.0 |
| **模型权重** | **BreezeBlue Research and Non-Commercial License** |

### 这意味着什么（重要）

- **模型权重是非商业许可**。如果你打算把这个项目用于商业用途，
  需要自己去确认语音模型部分的授权 —— 这一条不受本仓库的 MIT 影响。
- 本项目**没有**分发模型权重（3.5 GB），只调用使用者本地安装的实例。

---

## 3. SnowLuma

- **项目**：SnowLuma（QQ 协议端）
- **用途**：QQ 消息收发。本项目只调用它，不分发其源码。
- **注意**：请以其上游仓库的许可证为准。**本项目不承担对它的授权判断**，
  使用前请自行确认。

---

## 4. Live2D Cubism

- **项目**：Live2D Cubism SDK / 模型
- **用途**：桌宠形象渲染。
- **注意**：Cubism SDK 有**独立的专有许可**，模型素材通常也有各自的
  使用条款（很多禁止再分发）。本仓库**不包含**任何 Live2D 模型素材
  （5.5 GB，第三方版权）。

---

## 5. Python / Node 依赖

见 `requirements.txt`、`requirements-local-embed.txt`、
`gateway/memory/requirements.txt` 和 `desktop/desktop/package.json`。
各依赖按其自身许可证分发，均为宽松许可（MIT / BSD / Apache-2.0）。

主要几个：

| 依赖 | 许可证 |
|---|---|
| faiss-cpu | MIT |
| torch (CPU) | BSD-3-Clause |
| sentence-transformers | Apache-2.0 |
| BAAI/bge-large-zh-v1.5（嵌入模型） | MIT |
| Electron | MIT |
| pixi.js | MIT |

---

## 本项目自身

**MIT License** —— 见 [LICENSE](LICENSE)。

MIT 只覆盖本仓库里的代码。上面列出的第三方项目各有其许可证，
它们不因被本项目调用而变成 MIT。
