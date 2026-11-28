# 导出近 3 天诊断日志

在 `guard` 页面顶部点击「导出诊断日志（近3天）」，浏览器直接下载 ZIP。打包期间按钮禁用，完成或失败后自动恢复；登录失效、已有下载、宿主不支持下载时显示对应提示。提交问题时附上 ZIP、发生时间和异常现象。

范围由服务器固定为导出时刻之前 72 小时，时间使用 Unix 秒和 UTC。跨越边界的请求保留已有因果链；环境和设置是导出时刻快照。有记录不代表完整覆盖，升级前已清理的数据不能补回。

| 文件 | 内容 |
| --- | --- |
| `说明.txt`、`manifest.json` | 范围、条数、最早/最晚时间、缺失来源、损坏/超限计数及 SHA-256 |
| `environment.json`、`settings.json` | 本地版本、采集健康、集成状态及白名单设置；Provider 的 `api_type` 未声明时为 `null` |
| `runtime.jsonl` | 插件运行日志的级别和位置、请求收尾、提前停止、宿主停止传播和可关联的流水线异常 |
| `decisions.jsonl` | 当前文件、`.archive-*` 和旧 `.1` 中仍保留的判定；规则短路带实际输入长度、归一化长度和门槛 |
| `requests.jsonl` | 请求各步骤、原生事件类型和生效配置；优先读内存快照，已合并的分片保留原请求 ID |

排查一条无回复消息时，先看 `requests.jsonl` 的 `decision`、`request_hooks`、`main_model` 和 `lifecycle`。例如 `too_short` 同时记录 `normalized_length` 与当时生效的 `min_length`；`private_off` 是判定旁路，只有观察到 `main_model` 开始才说明宿主进入了主模型阶段。没有后续记录时查 `runtime.jsonl` 的 `host_stop`，再用 `flow_id` 查同一宿主事件的 LE 诊断包。分片合并可沿 `source_ids` / `merged_into` 找到原来各条事件，宿主重新创建事件时另有 `flow_id`。插件内的消息、会话 ID 使用各自的别名；跨插件只按共享的 `flow_id` 或可用的 `host_trace_id` 拼接。

不导出完整配置、数据库、聊天正文、判定输入、提示词、媒体、日志模板和异常原文。日志采集限插件自身及已识别的 AstrBot 流水线记录：LH/LE 的固定停止标记在宿主输出时可收录，其他插件的停止和宿主流水线的警告、错误需宿主日志携带已关联的追踪 ID。`environment.json` 的 `host_trace_enabled`、`collection.log_sinks` 和 `host_pipeline_capture` 表示采集条件，未开启或旧宿主不携带 ID 时，无法凭一个宿主停止标记认定是哪一轮。插件没收到的事件、停机前未落盘的记录和轮转删除的数据无法补回。

`diagnostic_bundle.py` 用标准库处理结构化记录和压缩；管理员 JWT 校验及流式响应使用宿主已有能力。`observability/guard_diagnostics.py` 负责数据白名单与来源，页面的 `diagnostics.mjs` 只调用宿主原生下载桥。接口 `GET console/diagnostics/export` 拒绝所有查询参数和资源令牌。导出不调用 Provider、模型列表或外部服务。

每插件诊断记录容量上限为 32 MiB，单文件达到 1 MiB 或跨小时后轮转。每条记录写入前检查总容量，不足时先删除最旧文件，为新记录腾出空间；超容量删除计数可见。启动、轮转、卸载及后台每 60 秒清理超过 72 小时的旧文件，无新日志时也会自动清理；导出还会逐条过滤时间。每次导出的原始内容预算为 64 MiB，超限只在完整记录边界截断。独立记录队列为 256 项，写入失败或队列溢出会计数，不影响业务提交。详细轨迹仍按原有 600 条上限保留；接收和收尾的简要记录保存在独立诊断日志中。

同插件一次只允许一个打包或下载任务。临时 ZIP 权限为 `0600`，目录为 `0700`；成功、发送异常、取消和卸载会清理文件并释放锁。启动时立即删除上次异常退出遗留的 ZIP；运行期间定期删除超过 1 小时的遗留 ZIP，不删除正在打包或下载的文件。诊断状态临时文件同时自动删除，不形成历史备份。清理仅作用于诊断目录内的 `records-*.jsonl`、`exports/*.zip` 和 `status-*.tmp`，不删除业务数据或原有日志。诊断存储不可写时页面会显示导出失败，不返回未脱敏内容。

验证使用合成日志、临时 SQLite、固定宿主和打桩 Provider，模型调用为 0：

```bash
python -B run_offline_tests.py
node --test tests/test_diagnostics.mjs
```

原生宿主与两页面联合验收入口位于 LE 的 `tests/console/diagnostics_browser_server.py` 和 `tests/ui/diagnostics-browser.test.mjs`。该层使用真实页面、宿主 SDK、管理员鉴权与下载端点；父页面是只提供下载的测试桥，不代表真实平台收发或生产部署。
