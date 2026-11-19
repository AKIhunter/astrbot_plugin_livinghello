# LivingHello

![LivingHello 像素机器人与消息判断](.github/img/Banner.png)

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.29.1-3a8060)](https://docs.astrbot.app/)
[![Version](https://img.shields.io/badge/version-2.9.0-3a8060)](CHANGELOG.md)

**简体中文** · [English](README.en.md)

让机器人先理解话题，在合适的时候接话。LivingHello先判断参与是否合适，
再交给AstrBot主模型和人格回复；支持连续消息合并、图片理解、分段、引用、语音和独立生图。
开发者：[AKIhunter](https://github.com/AKIhunter)；反馈QQ群：991350637。

## 能做什么

| 需求 | 功能 |
| --- | --- |
| 合适时接话 | 规则预筛、文字判定、视觉补证、语义否决和分数阈值 |
| 等你说完 | 同用户连续碎片合并，安静期与整批总等待上限 |
| 理解图片和媒体 | OCR文字证据、视觉观察、转发与引用链预算 |
| 回复更好读 | 段落与气泡、首条引用和@、标点清理、原生TTS |
| 按需画图 | 先回文字，再独立生成和发送；提示词映射、成功用词复用 |
| 记清图片结果 | 持久生成与发送回执；可按LE原事项ID领取和结算 |
| 看懂为什么没回 | 自动更新的判定、请求追踪、工具和发送阶段 |
| 使用背景与情绪 | 可选LivingMemory、LivingEmotion公开契约 |

<a id="quick-start"></a>

## 第一次使用

1. 在AstrBot≥4.29.1插件管理中通过本仓库地址安装，打开插件页面 `guard`。
2. 在「判定设置」选择小模型；先保留硬闸门 `enforce` 和阈值65。
3. 在「回复与私聊 → AstrBot配置人格」核对编辑配置，选择主模型与人格，保存。
4. 发送普通文字，在运行概览查看接收、判断、生成、工具和发送结果。
5. 再按需配置视觉、语音、生图、记忆和情绪接入。

判定模型留空会跟随会话模型。编辑哪份原生配置不会替代AstrBot的会话分配。
文字试跑会调用判定模型，不生成主回复、不发送消息。

<a id="modes"></a>

## 模式与发送边界

`enforce`静默时停止主回复；`shadow`记录判定并继续原生生成。两者都保留名单和明确禁聊限制。
原生@有直接路径，仍受碎片和发送检查。私聊是否评分由独立开关控制。
私聊窗口内的新消息可顶替尚未发送的旧回复；已发气泡不回收。
群聊水位默认不生效，总回复有效期仍优先检查。

<a id="image-generation"></a>

## 独立生图

在「回复与私聊 → 原生配置 → AI生图」选择实际支持生图的连接、协议、尺寸和每日额度。
主模型调用工具后先回文字，再发图片。`prepared`仅准备，`generated`仅生成，
`sent`记录发送接口受理，不表示用户已读。

映射开关、JSON规则和学习开关在同页设置；局部成功词对按实际连接、模型与协议复用。
LE可以对明确拒绝或超长描述有限调整等义词，改变场景须用户确认；未知请求不重复收费。
与LivingEmotion0.6.0配合时可按原promise_id结算，成功后后续轮次读取发送事实。

<a id="documentation"></a>

## 文档

| 要查什么 | 入口 |
| --- | --- |
| 控制台操作、模型人格、分段、试跑和排障 | [使用指南](docs/usage.md) |
| 每个配置项、默认值、保存位置和作用 | [配置参考](docs/configuration.md) |
| 技术原理与功能判断流程 | [流程说明](docs/architecture.md)与[判断逻辑](docs/decision-logic.md) |
| 图片映射、重试和成功用词 | [映射说明](docs/image-prompt-mapping.md) |
| 图片回执与原事项结算 | [回执契约](docs/image-delivery-receipts.md) |
| 开发、测试与接口 | [开发维护](docs/development.md) |
| 全部当前文档 | [文档索引](docs/README.md) |
| 历史变更 | [更新日志](CHANGELOG.md) |

判定、图片理解、文字试跑、TTS和生图可能产生所选Provider费用。
离线测试使用假模型和假发送，不代表真实模型效果或平台最终送达。
