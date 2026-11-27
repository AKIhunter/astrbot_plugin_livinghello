"""Text requests for deferred image prompts; never enter the main chat pipeline."""
from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
import copy
import inspect
import json
import sqlite3
import time

try:
    from ..integrations.guard_router_connections import CHAT_TYPES
except ImportError:
    from integrations.guard_router_connections import CHAT_TYPES

LEGACY = "@legacy"
DEFAULT_LIMIT = 48
MAX_DAILY_LIMIT = 1000
MAX_OUTPUT_TOKENS = 500
MAX_INPUT_BYTES = 32768
TIMEOUT = 30
ANSWER_RETENTION = 8 * 86400


class PromptTextUnavailable(Exception):
    pass


def _binding(provider):
    """Only use providers whose wire output and replay behavior we can bound."""
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

    output, input_chars = MAX_OUTPUT_TOKENS, MAX_INPUT_BYTES
    config = getattr(provider, "provider_config", {}) or {}
    if config.get("enable") is False or config.get("api_type", "chat") not in CHAT_TYPES:
        return None
    if type(provider).__name__ == "MultiApiRouterProvider":
        slot = getattr(provider, "pinned_slot", 0)
        upstream = next((u for u in provider.upstreams if u.slot == slot), None) if slot else None
        if upstream is None or upstream.api_type not in CHAT_TYPES:
            return None
        ceiling = getattr(provider, "max_output_tokens", output)
        input_ceiling = getattr(provider, "max_input_chars", input_chars)
        if type(ceiling) is not int or ceiling < 1 or type(input_ceiling) is not int or input_ceiling < 1:
            return None
        output, input_chars, provider = min(output, ceiling), input_ceiling, upstream.client
    if type(provider) is not ProviderOpenAIOfficial:
        return None
    model = provider.get_model()
    if (not isinstance(model, str) or not model.strip() or not provider.api_keys
            or not callable(getattr(provider.client, "with_options", None))):
        return None
    return provider, output, input_chars


def _supported(provider):
    try:
        return _binding(provider)
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


def _request(binding):
    live, output, _ = binding
    provider = copy.copy(live)
    provider.provider_config = dict(live.provider_config)
    # extra_body takes precedence at the wire: do not inherit messages, tools or n.
    original = provider.provider_config.get("custom_extra_body")
    extra = {key: original[key] for key in (
        "temperature", "top_p", "top_k", "min_p", "presence_penalty",
        "frequency_penalty", "repetition_penalty", "seed",
    ) if isinstance(original, dict) and key in original}
    extra.update(max_tokens=output, n=1)
    if "qwen" in provider.get_model().lower():
        extra.update(enable_thinking=False, chat_template_kwargs={"enable_thinking": False})
    provider.provider_config["custom_extra_body"] = extra
    provider.client = live.client.with_options(max_retries=0, timeout=TIMEOUT)
    provider.api_keys = live.api_keys[:1]

    async def no_retry(*args, **kwargs):
        raise PromptTextUnavailable("prompt_text_result_unknown") from None

    provider._handle_api_error = no_retry
    return provider


class ImagePromptText:
    def __init__(self, settings):
        self.settings = settings
        self.context = settings.context

    @contextmanager
    def _connect(self):
        if not self.settings.ledger:
            raise OSError("prompt_text_ledger_unavailable")
        with closing(sqlite3.connect(self.settings.ledger, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS prompt_text_migration "
                       "(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS prompt_text_calls "
                       "(request_id TEXT PRIMARY KEY, day TEXT NOT NULL, created REAL NOT NULL, answer TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS prompt_text_call_days ON prompt_text_calls(day)")
            db.execute("CREATE INDEX IF NOT EXISTS prompt_text_answer_age ON prompt_text_calls(created) "
                       "WHERE answer IS NOT NULL")
            yield db

    def targets(self):
        manager = getattr(self.context, "provider_manager", None)
        registered = getattr(manager, "inst_map", None)
        if isinstance(registered, dict):
            entries = registered.items()
        else:
            getter = getattr(self.context, "get_all_providers", None)
            entries = (((getattr(p, "provider_config", {}) or {}).get("id"), p)
                       for p in (getter() if callable(getter) else []))
        targets = []
        for pid, provider in entries:
            if not isinstance(pid, str) or not pid or pid == LEGACY:
                continue
            binding = _supported(provider)
            if binding:
                config = provider.provider_config
                targets.append(dict(id=pid, display=str(config.get("display_name") or pid),
                                    model=binding[0].get_model()))
        return targets

    def _legacy(self, read_legacy=None):
        with self._connect() as db:
            row = db.execute("SELECT payload FROM prompt_text_migration WHERE id=1").fetchone()
            if row or read_legacy is None:
                return json.loads(row[0]) if row else None
            # This callback only reads effective LE settings; never runs a model.
            old = read_legacy()
            if not isinstance(old, dict):
                return None
            ids = [old.get(key, "") for key in ("closing", "general", "fallback")]
            daily = old.get("daily_limit")
            if (any(not isinstance(pid, str) or len(pid) > 200 or pid == LEGACY for pid in ids)
                    or type(daily) is not int or daily < 0):
                return None
            snap = dict(zip(("closing", "general", "fallback"), ids),
                        daily_limit=min(MAX_DAILY_LIMIT, daily))
            db.execute("INSERT OR IGNORE INTO prompt_text_migration VALUES (1,?)", (json.dumps(snap),))
            row = db.execute("SELECT payload FROM prompt_text_migration WHERE id=1").fetchone()
            return json.loads(row[0])

    def _limit(self, legacy):
        value = self.settings.values["prompt_text_daily_limit"]
        if value is None:
            value = (legacy or {}).get("daily_limit", DEFAULT_LIMIT)
        return max(0, min(MAX_DAILY_LIMIT, value)) if type(value) is int else 0

    def usage(self):
        selector = self.settings.values["prompt_text_provider_id"]
        legacy = self._legacy()
        with self._connect() as db:
            used = db.execute("SELECT count(*) FROM prompt_text_calls WHERE day=?",
                              (self.settings.today(),)).fetchone()[0]
        return dict(used=used, limit=self._limit(legacy),
                    migration_pending=selector == LEGACY and legacy is None,
                    legacy_providers=[legacy[k] for k in ("closing", "general", "fallback")
                                      if legacy[k]] if selector == LEGACY and legacy else [],
                    follows_session=selector == "" or (selector == LEGACY and legacy is not None
                                    and not (legacy["closing"] or legacy["general"])))

    async def _candidate(self, route, selector, legacy):
        ids = [selector] if selector != LEGACY else [legacy["closing"], legacy["general"]]
        if not any(ids):
            getter = getattr(self.context, "get_current_chat_provider_id", None)
            if callable(getter):
                try:
                    current = getter(route)
                    ids.append(await current if inspect.isawaitable(current) else current)
                except Exception:
                    pass  # The optional, migrated fallback is still eligible.
        if selector == LEGACY:
            ids.append(legacy["fallback"])
        lookup = getattr(self.context, "get_provider_by_id", None)
        if not callable(lookup):
            manager = getattr(self.context, "provider_manager", None)
            lookup = getattr(manager, "get_provider_by_id", None)
        if callable(lookup):
            for pid in dict.fromkeys(p for p in ids if isinstance(p, str) and p):
                try:
                    provider = lookup(pid)
                    binding = _supported(await provider if inspect.isawaitable(provider) else provider)
                except Exception:
                    continue
                if binding:
                    return binding
        raise PromptTextUnavailable("prompt_text_model_unavailable")

    @staticmethod
    def _saved(db, key):
        row = db.execute("SELECT answer,created FROM prompt_text_calls WHERE request_id=?",
                         (key,)).fetchone()
        if row:
            if row[0] is None or row[1] < time.time() - ANSWER_RETENTION:
                raise PromptTextUnavailable("prompt_text_result_unknown")
            return row[0]
        return None

    async def complete(self, event, key, instruction, data, *, authorize, read_legacy):
        try:
            with self._connect() as db:
                saved = self._saved(db, key)
            if saved is not None:
                return saved
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if len((instruction + payload).encode("utf-8")) + 256 > MAX_INPUT_BYTES:
                raise PromptTextUnavailable("prompt_text_input_limit")
            self.settings.refresh()
            selector = self.settings.values["prompt_text_provider_id"]
            legacy = self._legacy(read_legacy if selector == LEGACY else None)
            if selector == LEGACY and legacy is None:
                raise PromptTextUnavailable("prompt_text_model_unavailable")
            binding = await self._candidate(str(event.unified_msg_origin), selector, legacy)
            if len(instruction) + len(payload) > binding[2]:
                raise PromptTextUnavailable("prompt_text_input_limit")
            try:
                provider = _request(binding)
            except Exception:
                raise PromptTextUnavailable("prompt_text_model_unavailable") from None
            if not await authorize() or self.settings.values["prompt_text_provider_id"] != selector:
                raise PromptTextUnavailable("prompt_text_authorization_changed")
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                saved = self._saved(db, key)
                if saved is not None:
                    return saved
                now, day = time.time(), self.settings.today()
                count = db.execute("SELECT count(*) FROM prompt_text_calls WHERE day=?", (day,)).fetchone()[0]
                if count >= self._limit(legacy):
                    raise PromptTextUnavailable("prompt_text_quota_exhausted")
                # Retain the claim after purging text so an old slot can't dispatch again.
                db.execute("UPDATE prompt_text_calls SET answer=NULL WHERE answer IS NOT NULL AND created<?",
                           (now - ANSWER_RETENTION,))
                db.execute("INSERT INTO prompt_text_calls VALUES (?,?,?,NULL)", (key, day, now))
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise PromptTextUnavailable("prompt_text_ledger_unavailable") from exc

        try:
            response = await asyncio.wait_for(provider.text_chat(
                prompt=payload, system_prompt=instruction, contexts=[], func_tool=None,
                request_max_retries=1), TIMEOUT)
            answer = getattr(response, "completion_text", None)
            if (getattr(response, "role", None) != "assistant" or not isinstance(answer, str)
                    or not answer.strip() or len(answer.encode("utf-8")) > MAX_INPUT_BYTES):
                raise PromptTextUnavailable("prompt_text_result_unknown")
            with self._connect() as db:
                db.execute("UPDATE prompt_text_calls SET answer=? WHERE request_id=?", (answer, key))
            return answer
        except Exception:
            # The claim is durable even if the SDK, provider or answer checkpoint fails.
            raise PromptTextUnavailable("prompt_text_result_unknown") from None
