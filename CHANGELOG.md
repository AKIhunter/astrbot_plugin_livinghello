# 更新日志

## 2.9.6 — 2026-11-23

### 修复

- 延迟图片门面保留权限、预检、失败原因码及异常类型，明确区分未请求失败和请求结果未知。
- LE 承诺图片遇到上游超时或未知结果时不再内部重新请求；明确内容拒绝仍按原描述最多尝试3次。
- 回执丢失和重复调用保持未知状态；复用事件时清除旧失败原因，避免串到下一次请求。
- 工具循环 logger 统一从 `astrbot.api` 导入。
- 修正文档静态提取遗漏“主模型超时切换”字段，同步当前提示词默认值的说明表。

### 兼容与验证

- 配套 LE 0.7.1；不改变已有生图配置、映射表、额度或成功词对账本。
- 双方门面与故障矩阵使用假模型、图片接口和平台；未调用付费接口。
- 详见[图片失败交接](docs/image-failure-followup.md)及[映射说明](docs/image-prompt-mapping.md)。

## 2.9.5 — 2026-11-20

### 修复

- 主模型默认15秒内无有效输出且已配置回退模型时，先取消主请求再直接切换，不叠加退避等待，仍占一次共享重试额度。
- 流式空心跳不算有效输出；首个非空正文、工具或消息链之后不再切换，避免混入重复回复。完整生成和回退模型仍受60秒调用预算限制。

### 新增

- 在“原生配置 → 模型”的“异常最大尝试次数”之后新增“主模型超时切换”，范围1–60秒，保存后新调用立即生效，页面自动读回并保留未保存草稿。
- 参数属于LH统一配置，不随所选AstrBot档案改变；需要开启“断线恢复 → 兜底回复”。旧配置缺项默认15秒，无需手动迁移，原有开关、撤回、名单和上下文保护继续生效。

用法、无回退模型时的行为与流式边界见[兜底说明](docs/reply_fallback.md)及[配置参考](docs/configuration.md)。

## 2.9.4 — 2026-11-20

### 修复

- 判定流水条数只控制参与判定输入；回复上下文条数独立控制主回复模型的LH额外历史，观察模式判为静默但继续生成时也生效。
- 两份历史在判定前各自取快照，当前消息单列。私聊开启参与判定后适用；默认私聊、直接@、直接称呼唤醒与灰名单明确呼叫保留原生路径。

### 调整

- 主回复流水改为短文本，会话场景只写一次，每行保留相对时间、昵称、真实作者ID、消息ID、引用、@和未解析媒体状态。
- 仅在同一请求已有完整正文副本时标注来源并省略重复正文；连续消息按来源排除批内历史，更早消息及其他作者的相同文字仍保留。
- 配置键、默认值、范围、原生历史和判定格式保持兼容，无需配置或数据库迁移。会话隔离、名单、有效期和字符预算继续生效。

用法与边界见[历史说明](docs/history-context.md#主回复模型的lh短流水)及[配置参考](docs/configuration.md#时限与预算如何配合)。
短格式可能容纳更多历史，不保证实际输入token按相同比例下降。

## 2.9.3 — 2026-11-20

### 修复

- 手工映射、LE局部替换/全文压缩、历史成功词对统一比较完整描述中的画面要点，避免绕过姿势、年龄、表情与衣着覆盖度检查。
- 兼容已知的仰卧同义朝向；跪姿变躺姿仍需确认，`nude`仅在明确颜色语境中豁免。
- 不合格的历史词对停止复用，保留原账本；配置、默认值和契约名称保持兼容。

### 新增

- 映射表按文件状态变化记录加载日志，包含文件名、mtime、来源、生效表规则数与固定失败原因。
- AI生图页面自动显示生效规则数、规则来源与失败原因；编辑中的草稿和保存版本保留。
- 补齐[加载与校验逻辑图](docs/image-prompt-mapping.md#加载与校验逻辑)及[交互HTML示例](docs/image-mapping-flow/index.html)。

要点检查只能发现已覆盖的明显变化，不能证明任意文本完全等义。LE仍使用自身收尾模型；
结果未知不自动补发，生图质量与真实模型成功率未纳入离线验收。

## 2.9.2 — 2026-11-19

- 生图提示词自动调整支持0–10次，默认2；页面、保存与运行时统一范围，配套LE 0.6.1共享剩余重试预算。
- 同步通用聊天说明与每段8–100字预置；仅逐字一致的旧内置文本跟随新版，手写文本与v3人物预置保留。
- 私人生图配置、映射表和成功学习数据库保留在本地，源码只包含公共机制与示例。
- 补齐配置参考、旧值迁移与分段回归；结果未知仍不重新生成。

## 2.9.1 — 2026-11-19

- 移除从绫注册表发现、输入接管、事件键、恢复契约、控制台兼容状态和专用测试文件。
  转发、引用、卡片与媒体理解由LH独立执行，原作者与当前正文保留。
- 人设隔离改用AstrBot原生配置、当前人格正文和会话指纹；缺少可信配置不开放历史补读。
- 故障恢复改用LH自有有界原消息保留与原生Agent/发送流程。
  过期和屏蔽不补答，仅生成不算送达，发送或工具副作用未知不重放。
- LE准备时间只读取LE事件计时；存量从绫私有键不再影响行为。
  历史日志保留，第三方背景块只作资料展示。LE / Memory可选契约继续使用。
- 当前手册修正第三方全局历史过滤与感知接管说明；旧实现仅在历史日志与Git中保留。
  迁移及验证边界见[独立实现](docs/independent-runtime.md)。
- 图片失败的立即询问门面测试显式关闭自动改词，避免默认重试许可与旧场景预期混用。

## 2.9.0 — 2026-11-19

- 图片生成与发送回执持久化，最近同会话事实用于后续追问；成功状态不可被旧失败降级。
- 通过 `livingemotion.image-result.v1` 收费前领取原图片承诺，发送成功仅完成原ID；
  未领取、来源变化或结果未知不再生成，明确失败沿用原任务进入有限改词链路。
- 配套LE 0.6.0；保留独立模式、每日额度、映射开关、失败通知和未知副作用保护。
- README仅介绍当前功能与结构；配置逐项用法从页面同源帮助生成，
  历史专题及旧验证记录统一归入本日志的原始来源索引。
- 归档12项不适用现行媒体降级行为的终止式测试草稿；
  [原件](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/tests/test_private_faults.py)
  仅用于历史追溯，现行媒体降级与管理员提醒回归保留。
- 验证采用离线规则、真实双插件门面、SQLite及全打桩上游/发送；
  不代表真实模型语义效果或生产已升级。当前契约见[回执说明](docs/image-delivery-receipts.md)。

记录当前独立维护仓库可核对的变化。历史版本以 Git 及迁移档案为准，不补造旧版本编号。

## 2.8.0 — 2026-11-19

### 新增

- 生图发送前应用词语、简单正则、去重补充和确认提醒；外部 JSON 自动重载，
  损坏时保留有效规则。默认描述 400 字建议、500 字硬上限，总长 900 字，只裁全局风格。
- 成功词对按实际连接、服务地址哈希、模型和协议持久记录并复用；明确拒绝降权，
  重复回执不累加，不记录完整提示词或密钥。
- 配套 **LivingEmotion 0.5.0**，明确内容拒绝或描述超长可为同一图片承诺最多调整两次
  等义措辞；改变画面需确认，生成或投递未知不补发。

### 兼容与验证

- 生图页新增四项平铺配置；旧 `image_generation.json` 自动补默认值，额度与已选连接保留。
  关闭映射沿用原描述与 4000 字上限；关闭学习保留历史但停止记录及复用。
- LH 离线全量实测 976 通过、35 个原有跳过，新增映射场景 24 项通过；
  原生配置页保存、读回及自然轮询 6 项通过，真实付费 API 测试为 0 次。
  详细格式与边界见[生图映射说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/image-prompt-mapping.md)。

## 2.7.11 — 2026-11-19

### 新增与修复

- 控制台可选择、编辑与重置预置提示词；平铺开关保留独立设置，升级后缺少的新字段使用默认值。
- 修复模型缓存遗漏配置变化、LE 历史视图与请求重试衔接；媒体失败继续降级回复，管理员提醒只发送普通文字。
- 保留技术正文、原生工具链与图片拒绝后的通知去重；私聊水位规则仍会丢弃已过时的未发送回复。
- 配套 LivingEmotion 0.4.2 的 `livinghello.shared-prompts.v1`；各自独立运行，未安装配套插件也可使用自身提示词。
- 离线回归 950 通过、35 跳过；旧的终止式私聊故障契约未实现，媒体降级另有专门测试。详见[提示词与边界说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/stability-2.7.11.md)。

## 2.7.10 — 2026-11-18

### 修复

- 生图失败的普通说明发送受理后，待办交接只保存后续状态，不再追加第二条等待文字。
  内容拒绝仍在同一条消息里询问调整或取消；502、超时等结果未知保留待核对状态。
- 保留重复通知拦截与发送未受理时不交接的行为。管理员模型故障提醒仍是独立通知。
- 新增后台 502、六类失败交接与发送未受理的离线回归；详见[失败通知说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/image-failure-notice.md)。

### 兼容

- 不新增配置项，不改变既有失败分类、每日额度或待办契约，升级无需迁移配置。

## 2.7.9 — 2026-11-18

### 修复

- 图片解码前检查源尺寸与实际像素，JPEG先安全降采样；PNG/GIF超限不再回退原图。
  最多两份图片同时解码，取消等待后仍由实际工作线程释放槽位。
- 平台OCR先按6000字预算截取再拼接，保留300行上限、置信度过滤和截断说明；
  图片下载失败仍保留已读文字，不调用视觉模型猜画面。
- 技术片段识别限制正文长度、JSON扫描量及尝试次数，修复长邮箱前缀和畸形JSON重复扫描。
  超过预算保留原文；禁忌词清理后的空白整理只作用于聊天片段，保留代码缩进。

### 配套与兼容

- 补齐公开版所需的平台OCR、图片链接刷新与下载入口；OCR不替代视觉理解。
  工具投递只清理 `plain` 正文，附件名称、地址和会话参数保留。
- 先按原标点分段，再清理每段；模型建议段长8–20字，不限制答案总字数或强拆代码。
  同步现有开关帮助，不新增配置或修改存量配置文件。
- 测试集中在 `tests/test_boundary_safety.py`；Pillow及真实宿主测试有显式依赖入口。
  新增30/30离线通过，Python3.12与3.11公开版基线对照无新增失败；既有失败与跳过分别记录。
  验收口径与剩余边界见[边界修复说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/boundary-safety.md)。

## 2.7.8 — 2026-11-18

### 新增

- 补齐内容拒绝、限流、设置问题、生成结果未知和发送未知的结构化分类及 LE 交接；
  普通回复、独立后台图片和到期图片共用受控失败信息，上游正文不进入用户说明或日志。
- 拍照、自拍、画图被内容规则拒绝后，主动问换人物、场景、风格还是取消。
  调整后仍被拒绝时再说明情况，让用户继续选择，用户可见文案不提任务、接口或内部编号。
- 已确认发出调整问句时交接 `adjustment_asked=true`，与 LE 0.4.1 协作持久去重；
  `followup_id` 只恢复用户明确调整过的原文生图，纯确认与未知结果不能强行恢复。
- 开启 LE 接入时读取 `livingemotion.image-policy.v1` 的关系许可；
  无法核验则婉拒，独立模式保留原情绪规则；固定工具说明不依赖可选提示词面板。

### 修复与兼容

- 发送异常停止原图重发，只有明确 `False` 才允许再投递同一张图一次；
  发送计划失败不能记成成功。到期生图直接执行，不再进入普通回复准备阶段。
- 无 LE 接入仍能说明失败，但不声称原图片已登记等待；完整恢复与取消需 LE **0.4.1**。
  不新增配置项、依赖或额度池，保留既有每日额度、超时和回复取消检查。
- LH 主动追问矩阵 8/8 离线实测通过（7 项独立用例 + 1 项显式双插件实际门面验收）。
  原生生图回归 56/56，通过更新 3 处旧发送预期核验新行为；真实付费 API 测试为 0 次。
  另有失败分类、到期执行及本地双协议前置验收 11/11 通过。
  详见[失败交接说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/image-failure-followup.md)。

## 2.7.7 — 2026-11-15

### 修复（2026-11-17）

- 修复配置页可选但运行时拒绝的视觉模型：路由统一以 `api_type=image_to_text/multimodal` 判断能力，锁定上游直连时继续保留声明，兼容 HY4 等未命中型号规则的模型。
- 识别嵌套合并转发中的 QQ XML 聊天记录卡片，有 `m_resid/resid` 时读取内部正文和图片，复用原有来源名单、去重、层数、条数及回拉预算。
- 缺少资源 ID、返回为空或回拉失败时明确标注内部未展开；卡片预览不当作正文，`m_fileName` 不作为资源 ID。
- 新增18项回归用例全部通过，全量同环境基线对照无新增失败或错误。此次不修改模型选择或展开配置；[修复记录](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/forward-xml-and-vision.md)包含原因、修复提交与验证边界。
- 文档同步更新中英文入门、配置参考和文档索引，并补齐配置表漏列的5个现有动图/视频字段。

### 改进

- 慢回复垫场句扩充为 12 句无标点短文案，按会话逐次轮换，各群独立循环，避免按分钟选句造成重复。灰名单明确呼叫、普通群 @/名字唤醒及自主参与放行共用句库；私聊仍由 `apply_to_private` 控制。
- 空配置或旧版默认三句自动使用内置新句库，自定义文案继续优先使用。发送等待、同群冷却及发送前保护保持原有配置；[垫场句说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/early-ack.md)记录配置位置、轮换和失败边界。
- 控制台首屏从 18–30 秒降到约 13 毫秒：缓存判据改为只读 provider 配置，不再逐项调用 `get_model()`，也不再拿 `id(provider)` 当指纹（provider 对象重建即导致缓存作废）。
- 模型目录不再在面板路径联网枚举 `get_models()`，新增 `PROVIDER_LIST_FAST` 开关（默认开启）；置为 `False` 恢复旧行为。
- 状态缓存有效期 1.5 秒 → 6 秒；参数与统计写入仍经 `_invalidate_state()` 即时作废。

### 说明

- 原生 provider 的下拉项不再显示「N 个可选模型」后缀，该信息原由联网枚举得出；路由项仍显示上游数量，路由上游锁定项不受影响。
- 新增 `_perf_selftest`（`PERF_SELFTEST_ONCE` 默认关闭）：置为 `True` 可在启动 60 秒后打印判据、模型目录与状态的冷热耗时。

## 2.7.6 — 2026-11-15

### 新增

- 动图按首帧理解：开启后 GIF 取第一帧转静态图再处理，链接形式也会下载转码；失败时回退原样透传。
- 视频理解（默认关闭）：对短视频抽帧后交给视觉模型描述，可配置最长处理时长与抽帧张数；取不到画面时明确告知模型不要猜测。

### 改进

- 最近判定在内存中只保留 100 条，轮转归档仅保留最近 3 个。
- 消息阶段与 LLM 阶段的静默出口统一记录原因，覆盖名单、投递失败、撤回、碎片取消、重复、冷却与故障冷却等分支，按「会话 + 原因」限流。
- 「内容理解」分组补充动图与视频相关配置及问号说明。

### 修复

- 夜间被作息状态拦下时，判定日志补记一条来源为对端的状态拦截记录，面板不再为空；同时补充 request_id 与中文原因，不再显示「未采集」。
- 生图接口 HTTP 错误改用插件自身的 logger。

### 文档

- 配套插件相关说明统一措辞，不再使用旧称。

## 2.7.5 — 2026-11-12

### 修复

- 普通群消息判定入口继续接收已有回复生成期间的新问题；在途状态只作为判定背景，主动回复冷却仍有效。
- 私聊关闭参与判定时，仍保存本轮图片摘要并挂载有范围限制的旧图读取工具；黑名单和撤回检查不变。
- 历史记忆补查只认真实历史内容；仅有当前情绪和关系状态时不复判，不把“知道情绪”误当作“知道往事”。
- 旧图读取在模型解析后、实际调用前及结果返回后重新检查会话、名单版本、撤回和本轮授权，过期结果不回填。

### 文档与验证

- 新增可离线打开的 [HTML 全流程](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/flow/index.html) 和 [四种接入组合说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/integrations-and-flow.md)。
- 合成请求经过实际钩子，覆盖独立、仅 LivingMemory、仅 LivingEmotion、两者接入及拒绝路径；12 项专项通过，外部网络连接被阻断。
- 同环境全量：修复前 752 项（680 通过、28 失败、16 错误、28 跳过），修复后 764 项（692 通过、28 失败、16 错误、28 跳过），失败和错误 ID 无新增。详见 [验证报告](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/validation-20261112.md)。

## 2.7.4 — 2026-11-12

### 新增

- `proactive_group_judge`：普通群消息的主动判定范围。`whitelist_empty`（默认）在群白名单为空时，让没有 @、没有称呼唤醒的群消息也补一次判定；`all` 不依赖白名单是否为空（名单外群仍只读静默，黑名单和灰名单除外）；`off` 不补，完全沿用宿主主动回复策略。
- 触发点在碎片合并完成之后，仍通过 `guard_llm_request` 走原有名单、灰名单、冷却与静默规则；同一批次只补一次，命中 `proactive_inflight_until` 或 `proactive_cooldown_seconds` 时跳过。
- 机器人自身消息、插件推送消息、通知事件、显式 @ 或称呼唤醒都不补判定；私聊行为不变。
- 判定为静默时保持 `enforce` 行为，不进入主模型生成。

### 成本提示

- 开启后每个群消息都会跑一次判定模型，成本随群消息量上升；判定预算、视觉预算与每小时上限照常生效。

## 2.7.3 — 2026-11-12

### 调整

- 水位语义反转：`private_watermark_window_seconds` 从“窗口内照发、窗口外丢弃”改为“窗口内顶替、窗口外照发”。窗口从消息到达时刻（`_at_guard_received_at`）起算，不按租约或主模型开始时间。
- 被顶替时丢弃正文并停止剩余分段气泡，已经发出的不回收；不调用 `request_agent_stop_all`，只阻止被顶替事件后续的视觉批次，已完成批次摘要保留。
- 300 秒回复机会有效期优先级高于水位：超期一律按 `message_expired` 丢弃，先于 `superseded_by_new_message` 判定。
- `private_watermark_window_seconds=0` 语义改为“关闭水位顶替，旧回复一律发送”（原为严格水位）。
- 首条为图片时也按同一规则顶替：轮次登记提前到输入解析、视觉理解之前。

### 新增

- `watermark_scope`：`private`（默认，仅私聊）/ `all`（私聊+群聊）；为 `all` 时 `private_watermark_window_seconds` 同时用于群聊。
- LivingEmotion 私聊分支先读 LivingHello 的发送检查，命中即丢弃已生成回复。
- 离线测试覆盖：水位内顶替、首图同规则、水位外照发、新消息携带旧上下文、300 秒优先级、分段停止与视觉批次停止。

### 已知限制

- 旧回复被丢弃后，宿主已写入历史的 assistant 文本无法安全移除，新请求可能仍看到它。
- 宿主 follow-up 捕获可能把新消息并入旧 run 的工具结果并丢失图片，本层不拦截捕获。

## 2.7.2 — 2026-11-11

### 新增

- 回复错误分类统一为 `classify(value) -> (category, disposition)`：`category` 只用于展示与日志，`disposition` 收敛为 `retry_then_rotate`、`rotate_now_report`、`not_an_error`、`fail_report` 四种处置，主模型重试层直接按处置分支，不再逐个状态码写判断。
- 配额关键字先于状态码判断，避免 403/429 同时可能是欠费时被误当成限流；4xx/5xx、超时、连接错误与协议异常分别映射到固定处置。
- `[ReplyFault]` 异常锚点，共 3 个调用点、3 个场景码：`ALARM_STALE_SUMMARY`（本轮已投递成功但 summary 仍为 failed）、`INTENTIONAL_COERCED_FAILED`（预期静默类别未被 finish 拦截、被改判为失败）、`CONTEXT_CHANGED_TYPEERROR`（旧闸门缺少 `_at_guard_judged_revision`，下一行将抛 TypeError）。每条带 category、disposition 与 request_id。
- `fragment_max_wait_seconds` 碎片批次总等待上限：从该批第一条消息到达时刻起算，默认 10 秒，范围 0–30 秒，`0` 表示不限制。续发只重置静默计时，不再延长总时长；到点即使用户仍在续发，也用当前已合并内容进入判断。
- 私聊水位宽限 `private_watermark_window_seconds`：默认 15 秒，生成开始后窗口内到达的新消息视为同一轮连发，已生成的回复照常发送；超过窗口才按新话题丢弃，`0` 恢复严格水位。该配置经 `livinghello.reply-freshness.v1` 契约下发给 LivingEmotion。

### 调整

- 控制台在碎片总上限与回复有效期不匹配时给出联动警告：有效期小于「静默等待 + 总上限 + 40 秒」时提示整批回复可能在发送前过期；只提示，不阻止保存，也不改写用户已保存的值。
- 碎片总上限为 `0` 时单独提示“续发可无限延长整批”，并建议设置有限上限。

验证为离线模型和平台替身；真实宿主路径由 `ASTRBOT_HOST_SOURCE` 可选检查。不以离线成功替代线上投递证据。[回复闭环说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/reply-health.md)、[碎片说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/fragments.md)。

## 2.7.1 — 2026-11-09

### 新增

- 主模型失败兜底开关，默认关闭，与判定断线恢复独立设置。
- 最多5次普通重试；失败后提醒管理员、等待60秒再进行最后一次尝试。工具后续步骤共享剩余预算，不重放工具或已尝试发送的结果。
- 请求追踪记录重试、等待、提醒与取消原因；兼容宿主停止接口检查。

### 修复

- 已放行群聊的首次模型调用与工具后的正常调用不再执行失败重试专用上下文检查；失败重试仍复核新消息、撤回、名单和过期。
- 保护取消通知当前 Agent 停止，只记录一次取消，避免空步骤重复刷满请求追踪。
- 异常摘录隐藏带引号的凭据，提醒额度持久化；健康监测超时与兜底总期限保持独立限制。

验证为离线模型和平台替身；真实宿主路径由 `ASTRBOT_HOST_SOURCE` 可选检查。不以离线成功替代线上投递证据。[兜底说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/reply_fallback.md)。

## 2.7.0 — 2026-11-04

### 新增

- “流程与试跑”一级页：判定与生成图、可查看依据的节点、文字试跑。
- 独立[判断逻辑说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/decision-logic.md)：逐步解释直通、本地规则、模型字段、补证、语义否决、阈值及模式差异。
- 可选“断线恢复”：文字判定 API 临时异常时通知管理员、冷却10分钟、自动探测，并由配套插件补看受影响历史。
- 原生主模型、回退模型和文本排版设置桥接，分组可展开并提供解释。
- 人格手动选择并保存后按原生配置固定，提供配套插件兼容读取接口。
- 独立 `livinghello_generate_image` 工具：主模型按需文生图或编辑本次输入/引用图片，复用原生模型或路由具体上游连接。
- 生图每日共享额度，默认关闭、每日20次；0禁止生图。调用前持久预占，失败或结果未知计已请求次数，不自动重试。

### 调整

- 参与模式收敛为硬闸门与观察模式；观察模式同步完整判断并记录，但不执行参与拦截。旧 `legacy/off` 配置自动迁移为硬闸门。
- “流程与试跑”明确区分判断结论与线上处理，观察模式判断静默时仍显示继续主回复。
- 最近判定详情使用紧凑多栏和技术健康状态色；原始输入按规则、实际输入、输出要求分段，支持复制到文字试跑。
- 正式名称改为 LivingHello，仓库更名为 `astrbot_plugin_livinghello`。
- 保留 `astrbot_plugin_at_guard` 技术 ID、旧数据目录及 API，无需迁移已保存数据。
- 上下文有效期0关闭时间过滤，按流水条数选消息；字符、会话隔离及回复机会有效期继续生效。
- 回退模型改为逐项下拉，支持添加、移除和排序；人格放到原生配置卡片顶部，编辑范围按需展开。
- 移除原生图片理解桥，统一使用独立内容理解的图片模型；原生 `t2i` 继续只负责文字排版转图。
- 文生图与图生图合并为独立能力，替换原 BiliBot 全局参数桥；不修改 BiliBot 配置。
- 汇总文档为 README 与 `docs/` 下使用、配置、流程、维护四份说明及索引；旧迁移清单归入 `docs/history/`。
- 测试模块集中到 `tests/`，保留根目录离线运行入口；补齐像素图标、Banner 和文档 CI。

### 本版本包含的既有修复

- 引用历史图片时由 LivingHello 使用自己的图片模型生成带来源摘要；配套插件只让位图片增强，避免重复调用。
- 模型段落与多消息发送独立控制，细化分段默认关闭，最大段数超出后合并正文。
- 成功查询证据复用和原生工具长期记忆写入提示，避免重复补查。
- 撤回保护限定于碎片等待窗口；最近判定支持私聊、明确呼叫和自主参与筛选。
- 配套插件准备计时适配、同会话图片观察缓存、原生保存冲突恢复和发送前过期检查。

以上按当前2.7.0实现汇总，包含同日已完成的增量更新。验证覆盖代码分支、接口和隔离发送；具体生图API的可用性和输出质量仍以实际服务为准。

## 历史专题与验证原件

旧专题的改动归入上面的版本记录，原文统一由以下固定Git来源追溯。
当前使用说明只在docs索引维护。旧验证数量仅证明当时范围，不能用于宣称
当前版本或生产通过；旧文内证据路径可能已撤下。原始文档也已本地独立归档。

| 原始专题 | 内容与追溯 |
| --- | --- |
| [LivingHello 文档](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/README.md) | 权威来源与更新规则；旧入口归并 |
| [流程与集成说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/architecture.md) | 判定流程；生成与发送；独立AI生图工具；碎片与撤回窗口；图片与查询证据；配套插件联用 |
| [2.7.9 边界修复](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/boundary-safety.md) | 实际规则；开关与兼容；离线验收；尚未修复的P2 |
| [LH 配置页面与问号说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/configuration-console.md) | 说明来源；自动刷新与保存；验证 |
| [配置参考](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/configuration.md) | 配置保存在哪里；插件参数；AstrBot 原生字段；AstrBot 原生分段；独立AI生图；请求诊断存储 |
| [控制台记录保留、能力耗时与分页面板](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/console-records.md) | 页面与保存；日志恢复与搜索；能力计时；部署注意 |
| [LivingHello 插件](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/current-features.md) | 快速使用；痛点；功能；行为；闸门模式；控制台 |
| [LivingHello 判断逻辑说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/decision-logic.md) | 先看结论；判定 API 断线恢复；总流程；第一步：明确呼叫与本地规则；第二步：组织判断上下文；第三步：文字模型初判 |
| [开发与维护](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/development.md) | 仓库布局；从完整克隆执行验证；UI 隔离回归；文档检查与发布；日志与指标；维护与恢复 |
| [垫场句轮换](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/early-ack.md) | 配置与触发范围；验证与维护 |
| [回复方式与原生分段](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/expression-takeover.md) | 回复方式；与AstrBot共用分段；模拟耗时与撤回；代码逻辑与职责；验证 |
| [合并转发与视觉能力修复](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/forward-xml-and-vision.md) | 问题与根因；视觉模型；XML 嵌套聊天记录；验证与边界；使用与排查 |
| [碎片消息等待与合并判断](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/fragments.md) | 总等待上限；判断与回复；代码顺序；边界与配置；验证 |
| [旧情绪与历史搜索输入](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/history-context.md) | 情绪；搜索；发送边界与限制；验证 |
| [历史图片的输入优化](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/history-images.md) | 每次请求如何处理；需要旧图细节时；预算与保存安全 |
| [历史迁移档案](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/history/README.md) | 历史迁移档案 |
| [migration-manifest.json](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/history/migration-manifest.json) | migration-manifest.json |
| [图片错误体分类](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/image-error-body-classification.md) | 图片错误体分类 |
| [图片失败后的调整与取消](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/image-failure-followup.md) | 开启与操作；分类与处理；交接与迁移；离线验收 |
| [生图失败只发送一次普通说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/image-failure-notice.md) | 生图失败只发送一次普通说明 |
| [生图提示词映射与成功用词](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/image-prompt-mapping.md) | 外部表与热更新；LE 承诺重试；成功记录与追踪；离线验证 |
| [从消息到发送：四种接入组合](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/integrations-and-flow.md) | 共同主流程；接入组合；图片与旧答；已知边界 |
| [黑白灰名单](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/message-policy.md) | 规则优先级；跨类型示例；填写、校验和移动；实时、历史和待发消息；LivingEmotion配套；人工复验 |
| [多图两张批次](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/multi-image-batching.md) | 多图两张批次 |
| [LivingHello 原生配置与流程说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/native-settings-flow.md) | 原生配置保存；人格固定；图片相关能力；流程节点；名称兼容 |
| [群聊回复、多图处理与异常提醒](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/optimizations-20261109.md) | 已生成回复的保护；多图与历史图片；异常列表与管理员私聊；官方模型名称；验证与回退 |
| [功能异常统一私聊](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/private-fault-notifications.md) | 功能异常统一私聊 |
| [私聊是否复用回复方式](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/private-reply-mode.md) | 私聊是否复用回复方式 |
| [回复发送、模拟耗时与撤回保护](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/reply-delivery.md) | 回复发送、模拟耗时与撤回保护 |
| [回复闭环与管理员提醒](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/reply-health.md) | 错误分类与处置；异常锚点 `[ReplyFault]`；水位顶替；状态与不变量；提醒路由与未知结果；数据与验证边界 |
| [主模型失败兜底](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/reply_fallback.md) | 调用顺序；保护条件；提醒与留痕；验证与限制 |
| [2.7.11 使用与边界说明](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/stability-2.7.11.md) | 2.7.11 使用与边界说明 |
| [使用指南](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/usage.md) | 打开控制台并保存；固定主模型与人格；设置语音回复；只限制流水条数；设置分段回复；等待碎片与撤回保护 |
| [LivingHello 2.7.5 合成数据验证](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/validation-20261112.md) | 反例与修复；同环境全量对照（实测）；场景覆盖；44 项既有失败 / 错误的处理；验证范围与运行方式 |
| [LivingHello 功能、边界与外部接口验收](https://github.com/AKIhunter/astrbot_plugin_livinghello/blob/d0c611073db5a0ca28642fe5ca8ca9214583075f/docs/validation-20261113.md) | 本次业务修复；遗留测试闭环；真实历史回放范围；对外接口与插件边界；复验 |
