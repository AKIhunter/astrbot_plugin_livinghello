# 图片错误体分类

HTTP 200/201 仅代表上游返回了响应。若正文存在 `error`，LivingHello 与非成功
HTTP 分支使用同一分类函数，不再先记录 `image_api=success`。

- `content_policy_violation`：`content_rejected`，交给 LivingEmotion 等待用户具体调整。
- 明确限流：`rate_limited`；明确请求、模型或认证错误：`provider_error`。
- 无法确定执行结果的错误、超时：`result_unknown`，继续禁止自动重发。

诊断仅保留最长 80 字的 code/type 标识，不输出上游 message、地址或密钥。
LE 的公开交接接口和失败类别保持兼容；本次没有增加内容拒绝自动重试。

`tests/test_image_failure_contract.py` 使用本机 HTTP 服务覆盖 images/chat_images
两协议及 200、201、400 状态，并验证每个请求只有一次上游调用。
