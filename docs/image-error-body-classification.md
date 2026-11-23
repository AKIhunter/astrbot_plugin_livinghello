# 图片错误体分类

HTTP 200/201 仅代表上游返回了响应。若正文存在 `error`，LivingHello 与非成功
HTTP 分支使用同一分类函数，不再先记录 `image_api=success`。

- `content_policy_violation`：`content_rejected`，本轮按原句最多追加两次请求，再交给 LivingEmotion 处理调整。
- 明确限流：`rate_limited`；明确请求、模型或认证错误：`provider_error`。
- 普通任务中，带 code/type 的明确错误体、超时、网络断开、5xx 或限流沿用本轮最多两次重试；耗尽后返回最终失败类别。混合内容拒绝时总请求最多五次。
- LE 0.7.1 承诺任务的超时、5xx、网络断开或其他未知结果立即停止内部重发，保留请求证据等待核对。
- 无 code/type 且无法确定执行结果：`result_unknown`，不自动重发；最终未知状态不能自行重画。

诊断仅保留最长 80 字的 code/type 标识，不输出上游 message、地址或密钥。
LE 的公开交接接口和失败类别保持兼容；LH 2.9.7没有增加此前的重试预算。
这些分类不能证明上游未收费；本地任务额度与实际HTTP请求计费分别核对。

`tests/test_image_failure_contract.py` 使用本机 HTTP 服务覆盖 images/chat_images
两协议及 200、201、400 状态；六个独立任务各三次受控拒绝，共18次本机请求和六次本地额度预占。
