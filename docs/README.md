# LivingHello 当前文档

适用2.9.0，2026-11-19核对。README只介绍当前产品；历史变更统一见[更新日志](../CHANGELOG.md)。
源码变化时同步权威文档，每次提交检查链接、默认值、版本、资产和离线回归。

| 用途 | 权威入口 | 依据 |
| --- | --- | --- |
| 操作、试跑、模型、人格、分段与排障 | [使用指南](usage.md) | pages/guard、原生桥与投递 |
| 每项配置默认值、位置、作用与例子 | [配置参考](configuration.md) | schema、页面帮助、保存校验、运行限幅 |
| 技术与判断流程 | [架构](architecture.md)、[判断逻辑](decision-logic.md) | main、gate、input、reply、images |
| 可离线打开的完整分支图 | [流程HTML](flow/index.html) | 当前规则与集成契约 |
| 独立、记忆、情绪、双接入 | [集成组合](integrations-and-flow.md) | 各方公开契约 |
| 生图映射、长度与学习 | [提示词映射](image-prompt-mapping.md) | ImagePromptMapping、LE修复契约 |
| 生图回执与原事项结算 | [发送回执](image-delivery-receipts.md) | SQLite回执、image-result.v1 |
| 图片预算与技术正文保护 | [资源边界](boundary-safety.md) | 解码、OCR、分段与正文保护 |
| 失败通知和需用户调整 | [通知](image-failure-notice.md)、[失败交接](image-failure-followup.md) | 失败类别、唯一通知与恢复 |
| 主模型失败与慢回复 | [兜底](reply_fallback.md)、[垫场](early-ack.md) | 有界重试与取消守卫 |
| 开发、接口与测试 | [开发维护](development.md) | tests、scripts、CI |

维护责任由对应功能修改者承担。文档核对的是仓库实现；生产部署、真实费用与模型效果另需证据。
超90天未复核，或接口/行为变化未更新时标记stale，不把历史报告当作当前验收。
