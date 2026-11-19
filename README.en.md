# LivingHello

![LivingHello pixel robot and message decisions](.github/img/Banner.png)

[![Version](https://img.shields.io/badge/version-2.9.0-3a8060)](CHANGELOG.md)

[简体中文](README.md) · **English**

Understand the topic before joining a conversation. LivingHello combines local rules,
text and visual participation decisions, then delegates replies to AstrBot's native model and persona.
It supports message fragments, image evidence, reply segments, quoting, TTS and separate image generation.

Install on AstrBot 4.29.1 or later and open `guard`. Select a classifier, keep `enforce` and threshold 65,
then choose the native reply model and persona and save. AstrBot still decides which configuration a conversation uses.

Image generation runs after the text reply. Persistent receipts distinguish preparation, generation and sending.
LivingEmotion 0.6.0 can bind and settle the original image promise. Successful substitutions are scoped to
the actual provider, model and protocol; unknown generation or delivery is not blindly repeated.
`sent` records sending-interface acceptance, not user read confirmation.

| Documentation | Contents |
| --- | --- |
| [Usage](docs/usage.md) | Console operations, native models, segments and diagnostics |
| [Configuration](docs/configuration.md) | Every field, default, location and effect |
| [Architecture](docs/architecture.md) | Message, decision and delivery flows |
| [Prompt mapping](docs/image-prompt-mapping.md) | Bounded repairs and learned substitutions |
| [Image receipts](docs/image-delivery-receipts.md) | Persistent facts and original promise settlement |
| [Development](docs/development.md) | Modules, interfaces and offline tests |
| [Documentation index](docs/README.md) | Current documents |
| [Changelog](CHANGELOG.md) | Historical changes |

Classifier, vision, TTS and image requests can incur provider charges.
Developer: [AKIhunter](https://github.com/AKIhunter). Feedback QQ group: 991350637.
