# LivingHello 当前文档

适用2.11.2，2026-11-28核对。README只介绍当前产品；历史变更统一见[更新日志](../CHANGELOG.md)。
源码变化时同步权威文档，每次提交检查链接、默认值、版本、资产和离线回归。

| 用途 | 权威入口 | 依据 |
| --- | --- | --- |
| 快速开始、试跑、模型、人格、分段与排障 | [使用指南](usage.md#打开控制台并保存) | pages/guard、原生桥与投递 |
| 每项配置默认值、位置、作用与例子 | [配置参考](configuration.md) | schema、页面帮助、保存校验、运行限幅 |
| QQ 名单规则与「戳一戳」豁免 | [消息名单](message-policy.md) | gate、消息与发送钩子 |
| 判定与回复历史条数、短流水与正文来源 | [历史上下文](history-context.md#主回复模型的lh短流水) | 独立快照、来源排除、短文本与字符预算 |
| 技术与判断流程 | [架构](architecture.md)、[判断逻辑](decision-logic.md) | main、gate、input、reply、images |
| 可离线打开的完整分支图 | [流程HTML](flow/index.html) | 当前规则与集成契约 |
| 独立、记忆、情绪、双接入 | [集成组合](integrations-and-flow.md) | 各方公开契约 |
| 生图映射、改词模型与额度、成功用词 | [提示词映射](image-prompt-mapping.md) | LH 生图执行、LE 承诺契约 |
| 映射加载、校验与生效状态交互示例 | [映射HTML](image-mapping-flow/index.html) | 离线夹具；六种加载状态与六种替换案例 |
| QQ 私聊即时单图 | [工具回复与生图](tool-turn-output.md) | 预检、承接语补写、先发文字再发图 |
| 生图回执与原事项结算 | [发送回执](image-delivery-receipts.md) | SQLite回执、image-result.v1 |
| 图片预算与技术正文保护 | [资源边界](boundary-safety.md) | 解码、OCR、分段与正文保护 |
| 失败通知和需用户调整 | [通知](image-failure-notice.md)、[失败交接](image-failure-followup.md) | 失败类别、唯一通知与恢复 |
| 主模型超时切换、失败与慢回复 | [兜底](reply_fallback.md)、[垫场](early-ack.md) | 可配置首个有效输出时限、有界重试与取消守卫 |
| 工具前后重复正文与新增内容 | [回复去重](reply-merge-dedup.md) | 请求局部契约、标点比较、装饰前去重与最终分段 |
| 导出近3天诊断包 | [诊断日志](diagnostics.md) | 受控采集、只读来源、管理员下载与清理 |
| 判断详情、功能终态、上下文与复验规则 | [详情与复验](decision-details.md) | 追踪收尾、只读规则接口、页面自动更新与原样复制 |
| 开发、接口与测试 | [开发维护](development.md) | tests、scripts、CI |
| 独立输入、人设隔离与故障补答 | [独立实现](independent-runtime.md) | 原生身份、RecoveryReplay |

维护责任由对应功能修改者承担。文档核对的是仓库实现；生产部署、真实费用与模型效果另需证据。
超90天未复核，或接口/行为变化未更新时标记stale，不把历史报告当作当前验收。
