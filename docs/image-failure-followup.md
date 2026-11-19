# 图片失败后的调整与取消

适用 **LivingHello 2.9.2 + LivingEmotion 0.6.1**。映射关闭、需改变画面或用尽次数后，
拍照、自拍、画图共用日常说明：

> 这张没能弄好，有些内容我暂时做不了。你想换个人物、场景或风格再试试，还是这次就算了？

具体改过画面仍被内容规则拒绝时：

> 按你刚才改的想法又试了一下，还是没能弄好，有些内容我暂时做不了。我先停在这里。你想再调整一下，还是这次就算了？

## 开启与操作

在「回复与私聊 → AI生图」配置并开启生图，在 LivingHello 的接入设置中开启
LivingEmotion；LE 保留承诺开关开启。关闭 LE 接入时仍会说明失败，
但无法依靠 LE 保留、恢复或取消原图片，也不会声称已登记等待。

用户给出具体新画面，例如“改成花园里穿外套的自拍”，可恢复原文生图。
纯“嗯 / 好的 / 继续 / 再试一次”不能恢复；“算了 / 不拍了 / 不画了”取消内容拒绝的等待图片。
多张等待时需明确选择。LE页面可取消等待或未知任务的后续处理，运行中不可取消；
取消不表示上游请求已撤销，也不再次生成或补发未知图片。
等待期限在LE设置为1–168小时，默认24小时；到期结束任务并保留原请求证据。
实际调用受主模型、关系许可、额度和发送守卫约束，不能把接受登记当作已经交付。

## 分类与处理

| 受控类别 | 行为 |
| --- | --- |
| `content_rejected` | 映射许可通过时沿原事项有限改词；否则等待具体调整或取消 |
| `rate_limited` | 文生图沿用 LE 五分钟延期；图生图保持等待 |
| `provider_error` | 先停下核对设置，不自动换上游或重试 |
| `result_unknown` | 核对是否生成过，禁止重复收费请求 |
| `delivery_unknown` | 核对已有图片是否收到，禁止盲目补发 |
| `quota_exhausted` | 说明当天数量已到上限；不占一次新请求额度 |

上游 `error.code=content_policy_violation` 识别为内容拒绝，429 为限流，
其他 4xx 为设置/请求问题，5xx、已请求后的超时及不明异常按结果未知处理。
上游响应正文只供分类，不进入模型、用户说明或日志。
已有图片只有明确 `False` 拒收才可再送同一张一次；异常、撤回与发送计划失败不盲目重发。

## 交接与迁移

`images/guard_image_failure.py` 提供分类，`guard_image_settings.py` 组织说明与发送；
`guard_image_contract.py` 保存工具固定说明，不依赖可选提示词面板。
`integrations/guard_livingemotion.py` 只通过版本契约调用 LE，不读取对方配置或数据库。

失败说明与调整问句只发一条，确认后把 `adjustment_asked=true` 交给
`livingemotion.image-followup.v1`。LE 持久保存已问状态，避免再问；
LH 不追加第二条“记下 / 等待”说明。发送失败或结果未知不声称已问成功。
普通回复、后台图片和到期图片沿用原归属，到期执行直接返回结构化失败，避免再新建一份。

生图工具的可选 `followup_id` 仅恢复当前会话中用户已具体调整的原文生图，
不新建、不立即重复生成。定时任务、参考图和未知结果不能用它强行恢复。
开启 LE 接入时用 `livingemotion.image-policy.v1` 核验关系许可，无法核验则婉拒；
关闭接入时仍用原独立情绪规则。

升级两插件并按宿主提示加载，原配置、额度和缓存继续沿用。
通知、回执和绑定字段按缺省迁移；升级前备份插件数据目录。
详细状态机见 [LE 承诺说明](https://github.com/AKIhunter/astrbot_plugin_livingemotion/blob/main/docs/promise.md)，
原事项收费前领取与结算见[回执](image-delivery-receipts.md)。

## 离线验收

历史计数与旧失败对照统一见[更新日志](../CHANGELOG.md)。
测试模型和发送均打桩，协议测试只用本地HTTP，不代表真实平台送达。

普通独立用例由现有 `run_offline_tests.py` 自动发现。
实际门面放在 `tests/promise_adjustment_handoff.py`，单独执行，
LH 独立 CI 不需要相邻 LE 仓库。显式验收需 Python 3.12、两仓库同级，
LE 目录名为 `astrbot_plugin_livingemotion`，并准备 AstrBot 的隔离源码和已有测试依赖：

```bash
export ASTRBOT_NATIVE_SOURCE=/path/to/astrbot
PYTHONPATH=".:tests:$(dirname "$ASTRBOT_NATIVE_SOURCE")" \
  python -m unittest test_promise_adjustment_notice promise_adjustment_handoff test_image_generation
PYTHONPATH=".:tests:$(dirname "$ASTRBOT_NATIVE_SOURCE")" \
  python -m unittest test_image_failure_contract
```

双插件用例固定隔离关系许可，以保证每次实际执行到内容拒绝。
冷启动等待预算为 5 秒，运行时关系核验 0.5 秒不变；此用例验证交接内容及去重，
不验收生产时延或已读状态。维护时应先比对同环境基线，再核对新用例实际走到的失败类别。
