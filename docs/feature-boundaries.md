# 功能边界、依赖与插件重叠

适用 LivingHello 2.11.1，2026-11-28 依据 README、配置 schema、控制台页面和运行代码核对。
本页回答三个问题：**这个功能做什么、什么时候该开、和另一个插件是否重复**。
字段默认值、位置和例子仍以[配置参考](configuration.md)为准。

## 先看结论

- LivingHello 负责**收到消息后要不要接、怎么接、图片怎么执行**，不主动发起聊天。
- LivingEmotion 负责情绪、关系、作息、主动聊天、承诺、日记与梦境；LivingMemory 负责长期记忆存储与召回。
- 支持 LivingHello + LivingEmotion 同时安装；不要再装第二个「回复闸门」「主动发送器」「长期记忆库」或「同一事项的生图执行器」。
- 安装不等于启用：`coupling_livingemotion_enabled`、`coupling_livingmemory_enabled` 默认关闭；开启后还要配套插件已激活、Provider 可用、会话范围合法。

## 功能清单

| 功能 / 入口 | 作用 | 什么时候该用 | 依赖 | 明确不负责 |
| --- | --- | --- | --- | --- |
| 闸门模式 `gate_mode` | `enforce` 按判定拦截；`shadow` 只记录仍生成 | 日常用 `enforce`；换模型或调阈值前用 `shadow` 跑 2–3 天对比 | AstrBot ≥4.29.1 | 不生成回复、不发送主动消息 |
| 参与判定 `classifier_provider_id` / 阈值 / 超时 | 主模型生成前判断是否值得接话 | 群聊抢话多、主模型成本高时 | 一个可用的文字判定模型 | 不评估图片、不写回复正文 |
| 零成本规则层 `gate_min_length` / `gate_hourly_budget` / 去重 / 冷却 | 先用规则拦掉短消息、纯表情、重复和超预算消息 | 建议始终开启，按群调整预算 | 无模型依赖 | 不做语义理解；不能替代判定模型 |
| 群消息主动判定入口 `proactive_group_judge` | 决定无 @ 普通群消息是否进入判定 | 需要管住群聊主动接话时；`off` 恢复宿主原生策略 | 判定模型、群名单规则 | 不是发送器；不会主动发消息 |
| 碎片合并 `fragment_wait_seconds` / `fragment_max_wait_seconds` / `fragment_wait_private` | 同一用户连续消息合并成一批 | 群聊碎片多、回复容易被拆散时 | 无模型依赖 | 不改变宿主原生分段设置 |
| 空 @ 回复 `empty_mention_mode` | 仅 @ 机器人且无正文时用固定文案或人格回复 | 希望空 @ 也有稳定响应时 | `fixed` 无模型；`persona` 用会话模型 | 不处理普通消息 |
| 私聊参与判断 `apply_to_private` | 私聊是否也走参与判定 | 默认关闭；私聊也需要控成本/控打扰时开启 | 判定模型 | 与碎片等待、撤回保护是独立开关 |
| 图片参与判断 `vision_shared_provider_id` / `gate_main_fallback_*` / `gate_vision_hourly_budget` | 文字判定需要看图时升级到视觉模型 | 群消息经常带图、需要判断图片是否相关时 | 支持图片的 Provider | 不生成图片；不把图片描述当已核实事实 |
| 上下文与记忆 `gate_context_messages` / `gate_context_max_age_seconds` / `gate_memory_timeout_seconds` / `participation_context_messages` | 控制判定流水、回复历史和可选记忆检索 | 需要更稳的话题理解时；按群调整 | 无模型依赖；记忆检索需 LivingMemory 接入 | 不改变宿主原生会话历史；不建立第二份长期库 |
| 回复整理与去重 `reply-merge-dedup` | 清理工具前后重复正文、合并分段与装饰 | 主模型/工具回复重复、分段混乱时 | 无额外插件；分段沿用宿主原生配置 | 不改变模型表达意图；不保证平台最终送达 |
| 独立生图 `image-prompt-mapping` / `tool-turn-output` / 发送回执 | 先回文字，再生成和发送图片；记录 `prepared/generated/sent` | 需要聊天中按需画图、复用成功用词时 | 生图 Provider；与 LE 承诺生图时需对应版本契约 | 不决定情绪/关系；不替 LE 登记承诺；`sent` 只表示接口受理 |
| 诊断包 `diagnostics` | 导出近 3 天脱敏诊断 ZIP | 排查判定、发送、工具问题时 | 管理员身份 | 不替代真实平台送达或模型效果验收 |
| 控制台与判定留痕 `decisions.jsonl` / 统计 | 看分数分布、最近判定、试跑与统计 | 调阈值、换模型、排查「为什么没回」时 | 无 | 不是生产监控；不等于模型正确率 |
| 可选接入 `coupling_livingemotion_enabled` / `coupling_livingmemory_enabled` | 读取 LE 情绪/关系、LM 记忆 | 已安装并配置好对应插件时 | 对应插件已激活、公开接口可用 | 关闭只关 LH 侧读取；不关闭对方自己的写入/归档 |

## 依赖关系

- 宿主：AstrBot ≥4.29.1；主模型、人格、TTS、分段和原生主动回复仍由 AstrBot 管理。
- 判定模型：`classifier_provider_id` 留空会跟随会话模型，不推荐；换模型后分数尺度会变，要重新标定阈值。
- 视觉与生图：图片判定需要支持图片的 Provider；生图需要实际可用的生图连接与额度。
- 可选插件：LivingEmotion、LivingMemory 均为可选；未安装或接口不可用时，LH 仍可独立运行。
- 无 Node/外部 CDN 依赖；生图映射、私人词对和学习数据留在本地。

## 与其它插件的重叠与取舍

### AstrBot 原生主动回复 / 定时主动任务

- 原生主动回复和定时任务由宿主决定何时生成/发送；LH 不替代它们。
- `proactive_group_judge` 只决定**收到的普通群消息**是否进入 LH 判定；`off` 时恢复宿主原生策略。
- LH 不会主动发消息，也不会修改宿主定时任务。若 LH 判定为静默，低分消息可能在主模型前被拦下，这是预期行为。

### 主动消息（Proactive Chat，DBJD-CR）

- 它是**主动发送器**：支持私聊/群聊主动消息、上下文感知、持久化、动态情绪、免打扰、TTS 和独立 WebUI。
- 与 LE 的重叠最大：LE 的「主动聊天」也会主动发送，并按情绪、关系、作息、额度、未回应退避和宿主问候让行。
- 取舍：
  - 要情绪/关系/作息驱动、和承诺/日记/梦境联动：**只用 LE 主动聊天**，关闭 Proactive Chat。
  - 要独立 WebUI、TTS 或纯主动消息功能：**只用 Proactive Chat**，关闭 LE 的主动聊天总许可，避免两个发送器。
  - 不要同时开启两个发送器：会重复发送、各自记账、冷却互不感知，用户侧看起来像机器人「抢话」。
- LH 不发送主动消息；Proactive Chat 的直接发送也不受 LH 闸门控制。不要指望 LH 的预算/冷却去约束另一个发送器。

### LivingEmotion

- LE 负责：情绪、关系、作息与精力、主动聊天、承诺、日记/梦境、记忆提供者、风格摘要。
- LH 负责：入站是否回复、回复整理、图片理解与生图执行、发送回执、诊断。
- 支持同时安装；通过 `coupling_livingemotion_enabled` 读取 LE 的情绪/关系/可用性。
- 重叠点与归属：
  - 回复生成：LE 注入状态，LH 决定是否让主模型生成；不要让第二个闸门插件再决定一次。
  - 图片承诺：LE 登记原承诺与结案，LH 领取、改词、生成和发送；不要再装第三个生图插件处理同一 `promise_id`。
  - 主动行为：LE 自己生成并提交，LH 的判定日志不能证明 LE 主动消息已送达。

### LivingMemory

- LivingMemory 负责长期记忆的存储、Embedding、召回、归档和衰减；LH 只在 `coupling_livingmemory_enabled=true` 时按当前会话授权召回。
- 不要同时启用两个会写入同一批对话的长期记忆插件；会造成重复写入、重复召回、费用和事实不一致。
- **关闭 LH 接入只关召回**：Memory 的原生归档和 `memorize_long_term_memory` 写入仍由 Memory 自己控制；要停止写入，去 LivingMemory 配置关闭写工具/归档。
- LH 不建立第二份长期向量库；短期证据缓存（内存、最长 5 分钟）不是长期记忆。

### 其它回复闸门、休眠/静默插件

- LH 判定钩子在 `on_llm_request`。如果另一个插件更早 `stop_event()`，消息到不了 LH，判定日志会是空的；这不是 LH 故障。
- 不要同时运行两个「是否回复」闸门；选择 LH 或另一个，把另一个关闭/卸载。
- 睡眠、免打扰、静默时段只保留一个负责人：LE 睡眠或宿主/其它插件，不要叠加多层拦截。

### 其它生图、提醒、情绪插件

- 生图：同一次请求只保留一个执行器；LE 承诺 + LH 执行是支持组合，不要再加第三个生图工具。
- 提醒/待办：LE 承诺负责到期执行；不要再装一个提醒插件对同一事项重复发送。
- 情绪/关系：LE 负责原生人格状态；另一个情绪插件同时写入会造成表达和数值冲突，二者选一。

## 配置归属速查

| 归属 | 负责内容 |
| --- | --- |
| AstrBot 宿主 | 人格、主对话模型、TTS、原生分段、原生主动回复/定时任务、Provider 凭据 |
| LivingHello | 闸门、碎片、空 @、私聊判定、图片理解、回复整理、生图执行、发送回执、诊断 |
| LivingEmotion | 情绪、关系、作息/精力、主动聊天、承诺、日记/梦境、记忆提供者、风格摘要 |
| LivingMemory | 长期记忆存储、Embedding、召回、归档与衰减 |

原则：**每个行为只在一个归属处配置**。不要在 LH 里配置 LE 的情绪，也不要用 LH 的开关代替 Memory 的写入开关。

## Agent 自检顺序

1. 先按上表确认功能归属，不要跨插件猜配置。
2. 检查插件列表：是否已经装了同类闸门、主动发送器、记忆库、情绪插件或生图工具。
3. 检查开关是否真的启用：LH/LE 接入默认关闭，LE 主动聊天还需总许可和群许可，记忆还需 Provider/权限。
4. 检查配置位置：情绪/关系/作息在 LE，记忆在 LivingMemory 或 LE 本地路径，回复闸门在 LH，人格/模型/TTS 在宿主。
5. 排查「为什么没回」：先看 LH 是否有 `guard_llm_request`；没有就检查是否有更早的插件停止事件。
6. 不要把 `prepared`/`generated` 当成已发送；只有实际发送接口受理才是 `sent`，且不等于用户已读。
7. 不确定时保持 fail-closed，并在报告里写清「未验证/未实测」边界。

## 相关文档

- [配置参考](configuration.md) · [使用指南](usage.md) · [四种接入组合](integrations-and-flow.md)
- [图片映射](image-prompt-mapping.md) · [发送回执](image-delivery-receipts.md) · [诊断日志](diagnostics.md)
- [平台功能与限制](usage.md#平台功能与限制)
