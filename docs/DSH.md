# Basic LivingHello for DSH · 0.1.0-alpha.1

独立原生 Host 插件，tag 为 `dsh/v0.1.0-alpha.1`。
基于 LH 2.9.7（`4553db05557362c106b85fb356511e8ba8cacb1d`），
复用原 GateEngine。仅将错误分类模块里的 AstrBot 消息类型导入推迟到
真正执行 AstrBot 通知时，分类规则和原宿主行为保持不变。

## 版本与安装

限定 DSH `0.2.1-rc.2`、Cordis `4.0.5`、schemastery `3.18.5`。
Node.js 22.19+、Python 3.11+；Python 仅标准库。
QQ 桥接按 `@xmanrui/dsh-im` 5.10.0 / 源码
`0694642e9fe5dd3af9a4f9516334d6ac288b2759` 评估。
配套 LE 使用 `@akihunter/dsh-livingemotion` `0.1.0-alpha.1`。
不声明其他 DSH RC / master 兼容。

在 DSH Host 依赖目录安装附件：

```sh
npm install /path/to/akihunter-dsh-livinghello-0.1.0-alpha.1.tgz
```

应用包内 `cordis.patch.yml`，或将其中 `id` / `name` 登记到 Host 插件配置。
本版未发布到 npm；使用本地附件。Python 不在桌面 PATH 时，在本插件
`python` 填写绝对路径。DSH 桌面安装 UI 尚未实测。

## 首版能力与准入

已接入 `agent/pre-step`，先等待下游结果；下游拒绝不会被改成放行。
仅对宿主已接受的 user-source 文字批次判定，工具和内部续步保留。
默认使用核心的长度与有效内容规则，零判定模型调用。
同一会话批次在当前进程最多缓存 256 项；重载后缓存清空。
缓存是避免重复判定，不是撤回已提交消息或取消平台发送的机制。

开启 classifierEnabled 后通过 DSH `llm.stream` 调用独立指定的
gateProvider / gateModel；原 GateEngine 控制解析、阈值、超时与格式重试，
失败关闭本次准入。最多两次格式调用；DSH Provider 自身重试策略另由宿主管理。
不替换主回答模型，不另行发送回复。

本版只处理桥接已送入 Agent 的明确请求，分类按直接会话请求语境执行。
不从正文猜群号、发言人、@ 或引用。无法取得被 QQ 桥接过滤的未提及消息。
媒体批次直接保留原 content 对象并交回 DSH，本版分类器不声称理解媒体；
图片 / 文件 / 工具引用的有效性由宿主负责。

最终回答、队列和超时补发属于 DSH-IM。未包含：完整群聊旁听、碎片窗口、
迟到回复作废、分段发送、主模型切换、原控制台和 AstrBot 数据迁移。
LE 动态情绪由 LE 自己注册；本版不额外读取或复制其私有配置。

## 平铺配置

| 字段 | 默认 | 效果与设置位置 |
| --- | --- | --- |
| enabled | true | 本插件关闭后不拦截、不启动 Python |
| python | python3 | 本插件填写 Python 3.11+ 可执行文件 |
| dataDir | .dsh/livinghello | 独立运行命名空间，不读取 AstrBot 配置 |
| classifierEnabled | false | 开启后在本插件填 gateProvider / gateModel，会产生额外模型消费 |
| gateProvider | 空 | DSH 已登记的 Provider ID |
| gateModel | 空 | 独立判定模型 ID |
| minLength | 2 | 0–20 整数；文字长度门槛 |
| threshold | 65 | 0–100 整数；模型参与阈值 |
| timeoutSeconds | 3.5 | 1–15 秒；分类整体期限，超时关闭本次准入 |

schema 与入口校验使用同一范围；非法配置拒绝启动，不悄悄夹取成另一值。
无需 Python pip 安装。分类未开启时不读取 Provider，也不调用付费接口。
默认短文字如“好”会被过滤；若要保留这类直接任务，在本插件将 minLength 调低。

## 生命周期、验证与回退

工作进程与模型 AbortController 由 Cordis effect 管理。
被取消步骤拒绝进入；卸载终止 Python、取消分类，不保留后台定时器或监听端口。
没有独立发送路径，因此不会与桥接产生第二份最终回答。

仓库执行 `npm test`。真实固定版本 Cordis / AgentRegistry / LlmRuntime，
伪 Agent、假模型适配器和真实 Python worker；零付费调用。
覆盖基础规则、分类允许/拒绝、缓存、并发历史隔离、下游对象保留、
工具/媒体透传、分类错误/格式错误/超时/取消、进程退出、配置范围。
真实 QQ 投递、完整 DSH AgentLoop、桌面安装 UI 未验证。

卸载本插件即可回退。AstrBot 根入口、配置和版本保持原样；
安装 DSH 包不表示可把它作为 AstrBot 插件加载。
