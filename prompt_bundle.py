"""Request-local built-in ownership; user-authored text is never deduplicated."""
import json
from pathlib import Path

DATA = json.loads(Path(__file__).with_name("prompt_bundle.json").read_text("utf-8"))
MARKER = "<!--lhle:builtins:v1-->"
MAX_CHARS = 10000


def variant_for(preset):
    return "v3" if preset == "sarcastic" else "v4"


def prompts(variant="v4"):
    return DATA["variants"][variant if variant in ("v3", "v4") else "v4"]


def valid_text(value, enabled=False):
    return (isinstance(value, str) and len(value) <= MAX_CHARS
            and (not enabled or bool(value.strip()))
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


def request_variant(request, proposed="v4"):
    # First contribution freezes the shared version for this request, including retries.
    if not hasattr(request, "_lhle_prompt_variant"):
        request._lhle_prompt_variant = proposed if proposed in ("v3", "v4") else "v4"
    return request._lhle_prompt_variant


def offer(request, owner, modules, text_part):
    """Replace one owner's contribution, then render each built-in ID exactly once."""
    offers = getattr(request, "_lhle_prompt_offers", None)
    if offers is None:
        offers = request._lhle_prompt_offers = {}
    offers[owner] = dict(modules)
    merged = {}
    for source in ("LH", "LE"):
        for key, value in offers.get(source, {}).items():
            if key in merged and merged[key] != value:
                raise ValueError("lhle_builtin_module_conflict")
            merged[key] = value
    parts = request.extra_user_content_parts
    parts[:] = [p for p in parts if not getattr(p, "text", "").startswith(MARKER)]
    if merged:
        body = "\n\n".join("[" + key + "]\n" + merged[key] for key in sorted(merged))
        part = text_part(MARKER + "\n" + body)
        marker = getattr(part, "mark_as_temp", None)
        if callable(marker):
            part = marker()
        else:
            # Bridges may supply a plain text DTO; preserve the same no-history contract.
            part.no_save = True
        parts.append(part)


def request_shared_settings(request, proposed=None):
    """Shared editable settings are frozen once, independent of LH/LE hook order."""
    if hasattr(request, "_lhle_shared_settings"):
        return dict(request._lhle_shared_settings)
    if proposed is None:
        return None
    modules = proposed.get("modules") if isinstance(proposed, dict) else None
    if (not isinstance(proposed, dict) or proposed.get("contract") != "livinghello.shared-prompts.v1"
            or not isinstance(modules, dict)
            or set(modules) != {"shared.context", "shared.memory"}
            or not all(valid_text(value) for value in modules.values())):
        raise ValueError("lhle_shared_settings_invalid")
    request._lhle_shared_settings = dict(modules)
    return dict(modules)


def shared_modules(variant, *, core, memory=False, settings=None):
    if settings is not None:
        return {key: value for key, value in settings.items()
                if value and (key != "shared.memory" or memory)}
    bundle = prompts(variant)
    result = {}
    if core:
        result["shared.context"] = bundle["shared_context"]
    if memory:
        result["shared.memory"] = bundle["lh_memory_write"] + "\n" + bundle["memory_selection"]
    return result
