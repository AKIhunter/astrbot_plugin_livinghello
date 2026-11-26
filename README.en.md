<div align="center">

# LivingHello

![LivingHello pixel robot and message decisions](.github/img/Banner.png)

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.29.1-3a8060)](https://docs.astrbot.app/)
[![Version](https://img.shields.io/badge/version-2.10.4-3a8060)](CHANGELOG.md)

[简体中文](README.md) · **English**

</div>

Understand the topic before joining a conversation. LivingHello combines local rules,
text and visual participation decisions, then delegates replies to AstrBot's native model and persona.
It supports message fragments, image evidence, reply segments, quoting, TTS and separate image generation.

Input understanding, persona isolation and outage replay are implemented within LivingHello.
See the [independent runtime guide](docs/independent-runtime.md) for freshness and delivery boundaries.

## 🎯 What it does

| Goal | Feature |
| --- | --- |
| Join a conversation at the right time | Local rules, text and visual classification, and a score threshold |
| Reply naturally | Fragment merging, quoting, bubbles, and native TTS |
| Find the right controls | Seven steps with direct links to the console settings |
| See why a reply did or did not arrive | Automatically refreshed decision, tool, and delivery trace |

<a id="quick-start"></a>

## 🚀 Quick start

1. Install on AstrBot 4.29.1 or later and open the `guard` page.
2. Open **快速开始** (Quick Start) and review the gate mode, classifier, reply scope, and native reply model.
3. Adjust reply behavior, media, integrations, and outage recovery as needed. Set the persona in AstrBot.
4. Save your changes, send a message in a test conversation, and check its decision and delivery in Recent Decisions.

> Quick Start opens the relevant setting without editing, saving, or running a probe. Open it again to resume where you left off.

**Contents:** [Modes](#modes) · [Image generation](#image-generation) · [Documentation](#documentation) · [FAQ](#faq) · [Compatibility](#compatibility) · [Release notes](#release-notes)

<a id="modes"></a>

## 🧭 Modes and delivery

`enforce` stops reply generation on a silent decision; `shadow` records the decision and continues native generation.
The classifier follows the conversation model when left unset. AstrBot assigns the actual native configuration to each conversation.
A text probe calls the classifier but does not generate or send a reply.

<a id="image-generation"></a>

## 🎨 Image generation

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

Open Prompt Injection to review managed presets and edit the ordinary prompts. Private image prompts, mapping files, and learned data remain local.

<a id="documentation"></a>

## ⚙️ Documentation

| Documentation | Contents |
| --- | --- |
| [Usage](docs/usage.md#打开控制台并保存) | Quick Start, native models, segments, and diagnostics |
| [Configuration](docs/configuration.md) | Every field, default, location and effect |
| [Architecture](docs/architecture.md) | Message, decision and delivery flows |
| [History context](docs/history-context.md#主回复模型的lh短流水) | Independent history limits and compact reply context |
| [Prompt mapping](docs/image-prompt-mapping.md) | Bounded repairs and learned substitutions |
| [Image receipts](docs/image-delivery-receipts.md) | Persistent facts and original promise settlement |
| [Diagnostic export](docs/diagnostics.md) | Last 72 hours, redaction, admin download and automatic retention |
| [Decision details and replay](docs/decision-details.md) | Trace status, original input/output, and replay |
| [Development](docs/development.md) | Modules, interfaces and offline tests |
| [Documentation index](docs/README.md) | Current documents |
| [Changelog](CHANGELOG.md) | Historical changes |

<a id="faq"></a>

## ❓ FAQ

- If a conversation still uses the old model after saving, check which configuration AstrBot assigned to it.
- If image generation or delivery is unknown, check the request and its receipt before trying again.
- See the [fallback guide](docs/reply_fallback.md) for model failure and retry behavior.

<a id="compatibility"></a>

## 🧩 Compatibility

Requires AstrBot 4.29.1 or later. LivingEmotion and LivingMemory are optional.

| Platform | AstrBot adapter | Scope |
| --- | --- | --- |
| QQ (OneBot v11, such as NapCat) | `aiocqhttp` | Group/private decisions, fragment merging and replies; QQ ID lists, admin notifications and recall protection |
| Telegram | `telegram` | Common decisions, fragment merging and replies; offline session-isolation and delivery-guard tests |
| Discord | `discord` | Common decisions and replies; offline delivery-guard tests |

Telegram and Discord have not completed live send/receive tests. Delivery depends on adapter capabilities, and QQ recall protection requires a OneBot recall notice during the fragment wait. See [platform features and limits](docs/usage.md#平台功能与限制). Use the [redacted diagnostic bundle](docs/diagnostics.md) when sharing support data; recorded classifier inputs can contain conversation text.

## ⌨️ Commands and interfaces

Open `guard` to configure the plugin. See [development and maintenance](docs/development.md) for management commands, permissions, and public interfaces.
Classifier, vision, TTS and image requests can incur provider charges.
Developer: [AKIhunter](https://github.com/AKIhunter). Feedback QQ group: 991350637.

<a id="release-notes"></a>

## 📜 Release notes

**2.10.4** adds seven Quick Start steps with 24 shortcuts. Decision details collapse listener coverage, show recorded feature timestamps, and display the retained classifier input and output. No configuration migration is needed. See [decision details and replay](docs/decision-details.md) and the [changelog](CHANGELOG.md).
