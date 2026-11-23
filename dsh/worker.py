"""GateEngine adapter with cancelable, correlated Node model callbacks."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import logging

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate.guard_engine import GateEngine

callbacks = {}
jobs = {}
counter = 0


def emit(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


class Provider:
    def __init__(self, parent, model):
        self.parent = parent
        self.model = model
        self.provider_config = {"id": "dsh-classifier", "model": model}

    def get_model(self):
        return self.model

    async def text_chat(self, **kwargs):
        global counter
        counter += 1
        callback_id = counter
        future = asyncio.get_running_loop().create_future()
        callbacks[callback_id] = future
        emit({"callback": "model", "id": callback_id, "parent": self.parent,
              "args": {"prompt": kwargs["prompt"], "system": kwargs.get("system_prompt", ""),
                       "maxTokens": kwargs.get("max_tokens", 512)}})
        try:
            result = await future
            return SimpleNamespace(completion_text=result, role="assistant")
        finally:
            callbacks.pop(callback_id, None)


async def handle(message, engine):
    identity = message["id"]
    try:
        args = message["args"]
        if message["op"] == "ping":
            result = {"ready": True}
        else:
            async def resolve():
                return Provider(identity, args["model"]) if args["classifierEnabled"] else None
            decision = await engine.decide(
                group_key=args["scope"], current_text=args["text"],
                context_lines=args.get("history", []), bot_line="DSH assistant",
                is_private=args["isPrivate"], resolve_provider=resolve,
                message_meta={"has_images": args["hasMedia"]}, record_result=False)
            result = decision.as_dict()
            # Basic mode uses the original cheap rules without hidden classification calls.
            if not args["classifierEnabled"] and decision.reason == "no_provider":
                result.update(action="reply", reason="basic_admission", source="rule")
    except asyncio.CancelledError:
        return
    except Exception:
        emit({"id": identity, "error": "gate_failed"})
        return
    finally:
        jobs.pop(identity, None)
    emit({"id": identity, "result": result})


async def serve():
    # Runtime config is fixed at worker startup; no AstrBot config is read.
    config = json.loads(sys.argv[2])
    engine = GateEngine({
        "gate_min_length": config["minLength"],
        "participation_confidence_threshold": config["threshold"],
        "classifier_timeout_seconds": config["timeoutSeconds"],
        "gate_total_timeout_seconds": config["timeoutSeconds"],
        "classifier_provider_id": config.get("gateModel", ""),
        "gate_upstream_fanout_enabled": False,
    }, None, logging.getLogger("dsh-lh"))
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            message = json.loads(line)
            if "callbackResult" in message:
                future = callbacks.get(message["callbackResult"])
                if future is not None and not future.done():
                    if message.get("error"):
                        future.set_exception(RuntimeError("model_failed"))
                    else:
                        future.set_result(message.get("result", ""))
            elif "cancel" in message:
                task = jobs.get(message["cancel"])
                if task:
                    task.cancel()
            else:
                task = asyncio.create_task(handle(message, engine))
                jobs[message["id"]] = task
    finally:
        for task in list(jobs.values()):
            task.cancel()
        await asyncio.gather(*list(jobs.values()), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(serve())
