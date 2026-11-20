# LivingHello

![LivingHello pixel robot and message decisions](.github/img/Banner.png)

[![Version](https://img.shields.io/badge/version-2.9.5-3a8060)](CHANGELOG.md)

[简体中文](README.md) · **English**

Understand the topic before joining a conversation. LivingHello combines local rules,
text and visual participation decisions, then delegates replies to AstrBot's native model and persona.
It supports message fragments, image evidence, reply segments, quoting, TTS and separate image generation.

Input understanding, persona isolation and outage replay are implemented within LivingHello.
See the [independent runtime guide](docs/independent-runtime.md) for freshness and delivery boundaries.

## Supported platforms

| Platform | AstrBot adapter | Scope |
| --- | --- | --- |
| QQ (OneBot v11, such as NapCat) | `aiocqhttp` | Group/private decisions, fragment merging and replies; QQ ID lists, admin notifications and recall protection |
| Telegram | `telegram` | Common decisions, fragment merging and replies; offline session-isolation and delivery-guard tests |
| Discord | `discord` | Common decisions and replies; offline delivery-guard tests |

Telegram and Discord have not completed live send/receive acceptance tests. Image, quote, mention and voice
delivery depend on the adapter. QQ recall protection requires a OneBot recall notice during the fragment wait.
Other AstrBot adapters use the common message interfaces but are not yet individually validated or declared.
See [platform features and limits](docs/usage.md#平台功能与限制).

## Quick start

Install on AstrBot 4.29.1 or later and open `guard`. Select a classifier, keep `enforce` and threshold 65,
then choose the native reply model and persona and save. AstrBot still decides which configuration a conversation uses.

Image generation runs after the text reply. Persistent receipts distinguish preparation, generation and sending.
LivingEmotion 0.6.1 can bind and settle the original image promise. Successful substitutions are scoped to
the actual provider, model and protocol; unknown generation or delivery is not blindly repeated.
`sent` records sending-interface acceptance, not user read confirmation.
Prompt adjustments allow 0–10 attempts, constrained by LE's remaining retry budget.
LE can close or cancel suspended tasks without repeating unknown image requests.
The console automatically shows active rule counts, their source and validation failures.
Manual rules, LE proposals and historical substitutions share the same scene checks.
See the [logic diagrams](docs/image-prompt-mapping.md#加载与校验逻辑) and
[interactive example](https://akihunter.github.io/astrbot_plugin_livinghello/image-mapping-flow/).

The editable general and segmentation presets now suggest 8–100 characters per chat paragraph.
Unedited shipped text follows the updated preset; custom text is preserved.
Private image prompts, mapping files and learned data remain local.

| Documentation | Contents |
| --- | --- |
| [Usage](docs/usage.md) | Console operations, native models, segments and diagnostics |
| [Configuration](docs/configuration.md) | Every field, default, location and effect |
| [Architecture](docs/architecture.md) | Message, decision and delivery flows |
| [History context](docs/history-context.md#主回复模型的lh短流水) | Independent history limits and compact reply context |
| [Prompt mapping](docs/image-prompt-mapping.md) | Bounded repairs and learned substitutions |
| [Image receipts](docs/image-delivery-receipts.md) | Persistent facts and original promise settlement |
| [Development](docs/development.md) | Modules, interfaces and offline tests |
| [Documentation index](docs/README.md) | Current documents |
| [Changelog](CHANGELOG.md) | Historical changes |

Classifier, vision, TTS and image requests can incur provider charges.
Developer: [AKIhunter](https://github.com/AKIhunter). Feedback QQ group: 991350637.

Version **2.9.5** switches to a configured fallback model when the primary produces no valid output within 15 seconds by default. With reply fallback enabled, set 1–60 seconds under “Native settings → Models → Primary model timeout”. Streaming stops being eligible for fallback after its first valid output.
See the [fallback guide](docs/reply_fallback.md) and [changelog](CHANGELOG.md).
