<div align="center">

# LivingHello

![LivingHello 像素机器人与消息判断](.github/img/Banner.png)

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.29.1-3a8060)](https://docs.astrbot.app/)
[![Version](https://img.shields.io/badge/version-2.10.4-3a8060)](CHANGELOG.md)

**简体中文** · [English](README.en.md)

</div>

让机器人先理解话题，在合适的时候接话。LivingHello先判断参与是否合适，
再交给AstrBot主模型和人格回复；支持连续消息合并、图片理解、分段、引用、语音和独立生图。
开发者：[AKIhunter](https://github.com/AKIhunter)；反馈QQ群：991350637。

输入理解、人设隔离和断线补答由LH独立实现；人格与发送沿用AstrBot原生链路。
恢复补答的有效期和未知发送边界见[独立实现说明](docs/independent-runtime.md)。

## 🎯 能做什么

| 需求 | 功能 |
| --- | --- |
| 合适时接话 | 规则预筛、文字判定、视觉补证、语义否决和分数阈值 |
| 等你说完 | 同用户连续碎片合并，安静期与整批总等待上限 |
| 理解图片和媒体 | OCR文字证据、视觉观察、转发与引用链预算 |
| 回复更好读 | 段落与气泡、首条引用和@、标点清理、原生TTS |
| 按需画图 | 先回文字，再独立生成和发送；提示词映射、成功用词复用 |
| 记清图片结果 | 持久生成与发送回执；可按LE原事项ID领取和结算 |
| 找到该配的设置 | 七步「快速开始」，点条目直达对应位置 |
| 看懂为什么没回 | 自动更新的判定、请求追踪、工具和发送阶段 |
| 使用背景与情绪 | 可选LivingMemory、LivingEmotion公开契约 |

<a id="quick-start"></a>

## 🚀 第一次使用

1. 在AstrBot≥4.29.1插件管理中通过本仓库地址安装，打开插件页面 `guard`。
2. 点顶部「快速开始」，依次核对闸门模式、判定模型、回复范围和 AstrBot 主模型。
3. 按需调整回复方式、分段、视觉、语音、生图、配套插件和故障提醒。人格在 AstrBot 面板设置。
4. 点「保存更新」，在测试会话发一条消息，到「运行概览 → 最近判定」确认判断、生成和发送结果。

> 引导条目只定位，不会自动修改、保存或试跑。再次点「快速开始」可回到原步骤。

**目录：** [模式](#modes) · [生图](#image-generation) · [文档](#documentation) · [常见问题](#faq) · [兼容性](#compatibility) · [更新日志](#release-notes)

<a id="modes"></a>

## 🧭 模式与发送边界

`enforce`静默时停止主回复；`shadow`记录判定并继续原生生成。两者都保留名单和明确禁聊限制。
原生@有直接路径，仍受碎片和发送检查。私聊是否评分由独立开关控制。
私聊窗口内的新消息可顶替尚未发送的旧回复；已发气泡不回收。
群聊水位默认不生效，总回复有效期仍优先检查。
判定模型留空时跟随会话模型；实际生效的原生配置由 AstrBot 分配。文字试跑调用判定模型，不生成主回复或发送消息。

<a id="image-generation"></a>

## 🎨 独立生图

在「回复与私聊 → 原生配置 → AI生图」选择实际支持生图的连接、协议、尺寸和每日额度。
主模型调用工具后先回文字，再发图片。`prepared`仅准备，`generated`仅生成，
`sent`记录发送接口受理，不表示用户已读。

映射开关、JSON规则和学习开关在同页设置；局部成功词对按实际连接、模型与协议复用。
页面自动显示生效规则数、规则来源与加载失败原因；手工规则、LE修复与历史词对共用画面要点校验。
查看[加载与校验逻辑图](docs/image-prompt-mapping.md#加载与校验逻辑)或打开[在线交互示例](https://akihunter.github.io/astrbot_plugin_livinghello/image-mapping-flow/)。
LE可以对明确拒绝或超长描述有限调整等义词，改变场景须用户确认；未知请求不重复收费。
与LivingEmotion0.6.1配合时可按原promise_id结算，成功后后续轮次读取发送事实。
LH改词次数支持0–10，实际受LE剩余自动重试预算限制；挂起任务可在LE页面取消。

「回复与私聊 → 提示词注入」可查看托管的预置提示词，按需编辑普通提示词。生图风格、私人映射表和学习数据留在本地。

<a id="documentation"></a>

## ⚙️ 文档

| 要查什么 | 入口 |
| --- | --- |
| 快速开始、模型人格、分段、试跑和排障 | [使用指南](docs/usage.md#打开控制台并保存) |
| 每个配置项、默认值、保存位置和作用 | [配置参考](docs/configuration.md) |
| 技术原理与功能判断流程 | [流程说明](docs/architecture.md)与[判断逻辑](docs/decision-logic.md) |
| 判定与回复历史条数、短流水格式 | [历史说明](docs/history-context.md#主回复模型的lh短流水) |
| 图片映射、重试和成功用词 | [映射说明](docs/image-prompt-mapping.md) |
| 图片回执与原事项结算 | [回执契约](docs/image-delivery-receipts.md) |
| 一键导出近3天的脱敏诊断包 | [诊断日志](docs/diagnostics.md) |
| 判断详情、自动刷新、原样复制与判定规则 | [详情与复验](docs/decision-details.md) |
| 开发、测试与接口 | [开发维护](docs/development.md) |
| 全部当前文档 | [文档索引](docs/README.md) |
| 历史变更 | [更新日志](CHANGELOG.md) |

<a id="faq"></a>

## ❓ 常见问题

- 映射表写坏后，查看AI生图状态栏的具体原因；修复JSON即可热恢复。同一路径优先保留本实例的有效旧表，首次无效或缺失时用内置规则。
- 生图超时或结果未知时不会自动补发；核对请求与发送回执后再决定下一步。
- 模型保存后会话未变化时，在AstrBot核对该会话的配置分配。更多排查见[使用指南](docs/usage.md#根据记录排查)。

<a id="compatibility"></a>

## 🧩 兼容性

需要AstrBot≥4.29.1；LivingEmotion、LivingMemory均为可选接入。生图承诺修复配套LE≥0.6.1。

| 平台 | AstrBot适配器 | 支持范围 |
| --- | --- | --- |
| QQ（OneBot v11，如NapCat） | `aiocqhttp` | 群聊与私聊的参与判断、碎片合并和回复；另有QQ号码名单、管理员QQ提醒和撤回保护 |
| Telegram | `telegram` | 通用参与判断、碎片合并和回复；已有离线会话隔离与发送保护测试 |
| Discord | `discord` | 通用参与判断和回复；已有离线发送保护测试 |

Telegram、Discord尚未完成真实平台收发验收。图片、引用、@和语音发送取决于适配器能力；
QQ撤回保护需收到OneBot撤回通知，并处于碎片等待窗口。
其他AstrBot平台使用通用消息接口，但尚未逐个平台验收，暂不列入支持声明。
详见[平台功能与限制](docs/usage.md#平台功能与限制)。分享排障资料时优先用[脱敏诊断包](docs/diagnostics.md)，注意判定原始输入可能含聊天正文。

## ⌨️ 使用指令

主要入口为插件页面 `guard`。管理命令、调用权限和公开接口见[开发维护](docs/development.md)；
模型在「回复与私聊 → 原生配置」设置，人格在 AstrBot 面板设置。

判定、图片理解、文字试跑、TTS和生图可能产生所选Provider费用。
离线测试使用假模型和假发送，不代表真实模型效果或平台最终送达。

<a id="release-notes"></a>

## 📜 更新日志

**2.10.4** 把「快速开始」扩为七步、24 个设置入口；判断详情收起监听覆盖，补充功能采集时间，并直接展示已留存的判定输入与输出。无需迁移配置。详见[详情与复验](docs/decision-details.md)和[更新日志](CHANGELOG.md)。
