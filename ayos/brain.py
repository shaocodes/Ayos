"""The part that decides what to do next.

LocalModelBrain asks a language model running on this computer (through Ollama, or any
OpenAI-compatible local server such as LM Studio). The model never writes commands. Each turn
it returns one small JSON object that picks a check from a fixed menu, or names a cause.

RuleBrain is the built-in fallback. It is used when no model is running, and it takes over a
single step whenever the model returns something unusable, so a session never gets stuck.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from .rules import CAUSES, NETWORK_CAUSES, PC_CAUSES, infer, next_check
from .tools import CHECKS

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma3:4b"

# The model server is on this computer. Never send these requests through a system proxy:
# one of the faults Ayos repairs is a broken proxy setting.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, payload=None, timeout: float = 120.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with _LOCAL.open(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


# ----------------------------------------------------------------- sorting the question
_NET = (
    "internet", "wifi", "wi-fi", "network", "website", "web site", "online", "offline", "connect", "connection", "browser",
    "page", "site", "dns", "router", "net ", "signal", "load", "chrome", "edge", "facebook", "youtube", "google", "lan",
)
_PC = (
    "slow", "lag", "hang", "freez", "storage", "disk", "space", "memory", "ram", "startup", "start-up", "boot",
    "bagal", "mabagal", "nagha", "loading forever", "takes forever", "sluggish",
)
_FAULT = (
    "not working", "isn't working", "isnt working", "doesn't work", "doesnt work", "won't", "wont", "can't", "cant", "cannot",
    "no internet", "not loading", "not opening", "not connect", "broken", "stopped", "down", "problem", "issue", "error",
    "fix", "help", "slow", "lag", "hang", "freez", "fail", "unable", "nothing loads", "keeps",
    "ayaw", "hindi", "di ", "wala", "walang", "sira", "bagal", "nawala", "ayusin",
)
_ASKING = ("what is", "what's", "what are", "how do", "how does", "how to", "why do", "why does", "explain", "ano ang", "ano yung", "paano")


def classify(question: str) -> dict:
    """Rough sort of the question. Used by the fallback and by one safety rule; the model does its own reading."""
    q = " " + question.lower().strip() + " "
    net = any(k in q for k in _NET) or bool(re.search(r"\b[a-z0-9-]+\.(com|net|org|ph|io|edu|gov)\b", q))
    pc = any(k in q for k in _PC)
    fault = any(k in q for k in _FAULT)
    asking = any(q.strip().startswith(k) for k in _ASKING)
    if asking and not fault:
        route = "general"
    elif pc and not net:
        route = "pc"
    elif net:
        route = "network"
    elif fault:
        route = "network"
    else:
        route = "general"
    # must_check: the complaint is about something Ayos has checks for, so advice without evidence is not acceptable
    return {
        "route": route,
        "is_fault": fault and not asking,
        "must_check": route in ("network", "pc") and (net or pc),
        "just_asking": asking and not fault,  # a question about computers, not a report of a problem
    }


# ----------------------------------------------------------------- prompt
# One line per check and per cause, written for the model. Short on purpose: on a PC without a graphics
# card the model reads only a few dozen tokens a second, so every line here costs time.
CHECK_HINTS = {
    "compare_with_normal": "what changed since the internet last worked on this PC",
    "check_adapters": "is the Wi-Fi or cable adapter switched on and connected",
    "check_ip_and_router": "does the PC have an address, and does the router answer",
    "check_internet_reach": "can internet addresses be reached by number (works even if DNS is broken)",
    "check_dns": "do the DNS servers answer (DNS turns website names into addresses)",
    "check_proxy": "is a proxy set, and do pages load with and without it",
    "check_hosts": "does the hosts file block a website",
    "test_website": "open a page the way a browser would",
    "check_disk": "free storage space",
    "check_memory": "free memory and the apps using the most",
    "check_startup": "apps that start with Windows",
}
CAUSE_HINTS = {
    "adapter_disabled": "the adapter is turned OFF in Windows",
    "wifi_not_connected": "the adapter is on but not joined to any network",
    "no_ip_address": "connected, but the router gave the PC no address",
    "router_unreachable": "the PC has an address but the router does not answer",
    "isp_outage": "the router answers but nothing beyond it can be reached",
    "dns_misconfigured": "DNS was set BY HAND to a server that does not answer",
    "dns_server_down": "DNS is on AUTOMATIC but the router's DNS does not answer",
    "proxy_blocking": "a proxy is ON and pages load only without it",
    "hosts_block": "the hosts file blocks the website",
    "no_fault_found": "every check passes and a test page loads",
    "disk_full": "storage is almost full",
    "low_memory": "memory is almost full",
    "many_startup_apps": "too many apps start with Windows",
    "pc_looks_healthy": "storage, memory and start-up apps are all fine",
}


def system_prompt(compact: bool = False, route: str | None = None) -> str:
    """The model's instructions.

    compact=True is used when memory has already run the checks that matter and the results prove a
    cause: the model only has to read them and name it, so the list of checks and the rules for
    investigating are left out. On a PC without a graphics card that saves several seconds of reading.
    """
    cause_ids = PC_CAUSES if route == "pc" else NETWORK_CAUSES if route == "network" else NETWORK_CAUSES + PC_CAUSES
    causes = "\n".join(f"{cid}: {CAUSE_HINTS[cid]}" for cid in cause_ids)
    if compact:
        return f"""You are Ayos, a PC repair technician running offline on the user's own Windows PC.
Checks were already run on this PC. Read the results and name the cause. A safety step verifies your conclusion.

Reply with ONE JSON object on one line:
{{"thought": "at most 15 words: what the results show", "action": "conclude", "cause": "one cause id", "message": "what to tell the user"}}

Causes:
{causes}

message: two short plain sentences. Say what is wrong and why it causes what the user sees. Use the user's language."""
    checks = "\n".join(f"{name}: {CHECK_HINTS[name]}" for name in CHECKS)
    causes = "\n".join(f"{cid}: {CAUSE_HINTS[cid]}" for cid in NETWORK_CAUSES + PC_CAUSES)
    return f"""You are Ayos, a PC repair technician running offline on the user's own Windows PC.
Find the cause by running checks, one per turn, then name the cause. You cannot change anything yourself. A safety step verifies your conclusion.

Reply with ONE JSON object on one line:
{{"thought": "at most 15 words: what the last result means", "action": "a check name, or conclude, or answer", "cause": "a cause id when concluding, otherwise none", "message": "what to tell the user when concluding or answering, otherwise empty"}}

Checks:
{checks}

Causes:
{causes}

Rules:
- A problem on this PC: run checks, never guess. Conclude only what the results show.
- If a setting changed since it last worked, check that first. Otherwise work outward: adapter, address and router, internet line, DNS, proxy, hosts file.
- Never repeat a check. Stop as soon as a result shows the cause.
- A general question that needs no check, or a problem you have no check for (printer, sound, one app): use answer.
- message: two short plain sentences. Say what is wrong and why it causes what the user sees. Use the user's language."""


def decision_schema(allowed_checks: list, must_conclude: bool = False, answer_only: bool = False, causes=None) -> dict:
    if must_conclude:
        actions = ["conclude"]
    elif answer_only:
        actions = ["answer"]
    else:
        actions = list(allowed_checks) + ["conclude", "answer"]
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string"},
            "action": {"type": "string", "enum": actions},
            "cause": {"type": "string", "enum": list(causes or NETWORK_CAUSES + PC_CAUSES) + ["none"]},
            "message": {"type": "string"},
        },
        "required": ["thought", "action", "cause", "message"],
    }


def first_message(question: str, notes: list) -> str:
    text = f'The user says: "{question.strip()}"'
    for n in notes:
        if n:
            text += "\n" + n
    return text


def peek(text: str) -> dict:
    """Read what can already be read from a reply that is still being written (unfinished JSON)."""
    out = {}
    for key in ("thought", "action", "cause", "message"):
        m = re.search(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)', text or "", flags=re.S)
        if not m:
            continue
        frag = m.group(1)
        for cut in (frag, frag[:-1]):  # a half-written escape such as a lone backslash is dropped
            try:
                out[key] = json.loads('"' + cut + '"')
                break
            except ValueError:
                continue
    return out


def parse_decision(text: str):
    """Pull the JSON object out of the model's reply. Returns a dict or None."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        obj = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            return None
    if not isinstance(obj, dict) or not isinstance(obj.get("action"), str):
        return None
    return {
        "thought": str(obj.get("thought") or "").strip()[:400],
        "action": obj["action"].strip(),
        "cause": str(obj.get("cause") or "none").strip(),
        "message": str(obj.get("message") or "").strip()[:1500],
    }


# ----------------------------------------------------------------- fallback
class RuleBrain:
    kind = "rules"
    label = "Built-in rules (no language model)"
    model = ""

    def available(self) -> bool:
        return True

    def start(self, question: str, notes: list, compact: bool = False, route: str | None = None) -> list:
        return []

    def observe(self, convo: list, text: str) -> None:
        pass

    def record(self, convo: list, decision: dict) -> None:
        pass

    def say(self, convo: list, question: str) -> dict:
        return {"ok": False, "text": "Follow-up questions need the language model, and it is not running right now.", "seconds": 0.0}

    def step(self, convo: list, view: dict) -> dict:
        route = view["route"] if view["route"] in ("network", "pc") else None
        if route is None:
            return {
                "action": "answer",
                "cause": "none",
                "thought": "",
                "message": "I can answer general questions only when the language model is running. "
                "Right now I can still check this PC: tell me what is not working, for example 'my internet is not working' or 'my PC is slow'.",
                "source": "rules",
                "seconds": 0.0,
            }
        cause = infer(view["obs"], route)
        if cause and not (cause in ("no_fault_found", "pc_looks_healthy") and next_check(view["obs"], route, view["has_baseline"], view.get("prefer", ()))):
            return {"action": "conclude", "cause": cause, "thought": "", "message": "", "source": "rules", "seconds": 0.0}
        nxt = next_check(view["obs"], route, view["has_baseline"], view.get("prefer", ()))
        if nxt:
            return {"action": nxt, "cause": "none", "thought": "", "message": "", "source": "rules", "seconds": 0.0}
        return {"action": "give_up", "cause": "none", "thought": "", "message": "", "source": "rules", "seconds": 0.0}


# ----------------------------------------------------------------- local language model
class LocalModelBrain:
    """A language model on this computer. api='ollama' or 'openai' (LM Studio, llama.cpp server, and others)."""

    kind = "model"

    def __init__(self, model: str = DEFAULT_MODEL, url: str = DEFAULT_URL, api: str = "ollama", timeout: float = 180.0):
        self.model = model
        self.url = url.rstrip("/")
        self.api = api
        self.timeout = timeout
        self.use_think_flag = True
        self.last_speed = None  # tokens per second from the most recent reply
        self.fallback = RuleBrain()

    @property
    def label(self) -> str:
        return f"{self.model} (local, via {'Ollama' if self.api == 'ollama' else 'OpenAI-compatible server'})"

    # ---- server
    def list_models(self, timeout: float = 2.0) -> list:
        try:
            if self.api == "ollama":
                data = _http_json(self.url + "/api/tags", timeout=timeout)
                return [m.get("name") for m in data.get("models", []) if m.get("name")]
            data = _http_json(self.url + "/v1/models", timeout=timeout)
            return [m.get("id") for m in data.get("data", []) if m.get("id")]
        except Exception:
            return []

    def server_up(self, timeout: float = 2.0) -> bool:
        try:
            _http_json(self.url + ("/api/tags" if self.api == "ollama" else "/v1/models"), timeout=timeout)
            return True
        except Exception:
            return False

    def available(self) -> bool:
        models = self.list_models()
        return any(m == self.model or m.split(":")[0] == self.model for m in models) if models else False

    def warm_up(self) -> dict:
        """Load the model into memory and let it read the instructions once, so the first real question is fast."""
        started = time.time()
        try:
            # The instructions used most often: an internet problem where memory already found what changed.
            self._chat(
                [{"role": "system", "content": system_prompt(True, "network")}, {"role": "user", "content": 'The user says: "hello"'}],
                decision_schema([], must_conclude=True),
                max_tokens=1,
            )
            return {"ok": True, "seconds": round(time.time() - started, 1)}
        except Exception as e:
            return {"ok": False, "seconds": round(time.time() - started, 1), "error": _short_error(e)}

    # ---- one request
    def _chat(self, messages: list, schema: dict, max_tokens: int = 260, on_text=None) -> dict:
        """One reply from the model. With on_text, the reply is streamed and on_text(text_so_far) is called as it grows."""
        if self.api == "ollama":
            body = {
                "model": self.model,
                "messages": messages,
                "stream": on_text is not None,
                "format": schema,
                "keep_alive": "60m",
                "options": {"temperature": 0, "seed": 7, "num_ctx": 4096, "num_predict": max_tokens},
            }
            if self.use_think_flag:
                body["think"] = False
            req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
            try:
                res = _LOCAL.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if self.use_think_flag and "think" in detail.lower():
                    self.use_think_flag = False  # this model has no thinking switch
                    return self._chat(messages, schema, max_tokens, on_text)
                raise RuntimeError(f"model server said {e.code}: {detail}") from None
            text, data = "", {}
            with res:
                if on_text is None:
                    data = json.loads(res.read().decode("utf-8"))
                    text = (data.get("message") or {}).get("content", "")
                else:
                    for raw in res:  # one JSON object per line while the model writes
                        raw = raw.strip()
                        if not raw:
                            continue
                        part = json.loads(raw.decode("utf-8"))
                        if part.get("error"):
                            raise RuntimeError(str(part["error"])[:300])
                        piece = (part.get("message") or {}).get("content") or ""
                        if piece:
                            text += piece
                            try:
                                on_text(text)
                            except Exception:
                                pass
                        if part.get("done"):
                            data = part
                            break
            tokens = int(data.get("eval_count") or 0)
            dur = float(data.get("eval_duration") or 0) / 1e9
            return {
                "text": text,
                "tokens": tokens,
                "tok_per_s": round(tokens / dur, 1) if dur > 0 and tokens > 1 else None,
                # how much of the conversation the server had to read again (small when its cache is reused)
                "prompt_tokens": int(data.get("prompt_eval_count") or 0),
                "prompt_seconds": round(float(data.get("prompt_eval_duration") or 0) / 1e9, 2),
                "gen_seconds": round(dur, 2),
            }
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": "ayos_step", "strict": True, "schema": schema}},
        }
        started = time.time()
        try:
            data = _http_json(self.url + "/v1/chat/completions", body, self.timeout)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"model server said {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        tokens = int((data.get("usage") or {}).get("completion_tokens") or 0)
        dur = time.time() - started
        return {"text": text, "tokens": tokens, "tok_per_s": round(tokens / dur, 1) if dur > 0 and tokens > 1 else None}

    # ---- conversation
    def start(self, question: str, notes: list, compact: bool = False, route: str | None = None) -> list:
        return [{"role": "system", "content": system_prompt(compact, route)}, {"role": "user", "content": first_message(question, notes)}]

    def observe(self, convo: list, text: str) -> None:
        """Add a result for the model to read. Messages are only ever added, so the server can reuse its cache."""
        if convo and convo[-1]["role"] == "user":
            convo[-1] = {"role": "user", "content": convo[-1]["content"] + "\n" + text}
        else:
            convo.append({"role": "user", "content": text})

    def record(self, convo: list, decision: dict) -> None:
        """Keep the conversation in step when a decision was made for the model (first step, safety check, fallback)."""
        convo.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {"thought": decision.get("thought", ""), "action": decision["action"], "cause": decision.get("cause", "none"), "message": ""}
                ),
            }
        )

    def say(self, convo: list, question: str, on_text=None) -> dict:
        """Answer a follow-up question about this session in plain words. Changes nothing and runs no checks."""
        started = time.time()
        ask = (
            f'The user now asks a follow-up question: "{question.strip()}"\n'
            "Answer it in two to four short sentences, in plain words, in the language they used. "
            "Base it on the check results above. Do not tell them to type commands. "
            'Reply as JSON: {"message": "your answer"}'
        )
        schema = {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}
        try:
            out = self._chat(convo + [{"role": "user", "content": ask}], schema, max_tokens=320, on_text=on_text if self.api == "ollama" else None)
        except Exception as e:
            return {"ok": False, "text": "The language model could not answer: " + _short_error(e) + ".", "seconds": round(time.time() - started, 2)}
        text = out["text"] or ""
        try:
            obj = json.loads(re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip())
            text = str(obj.get("message") or "") if isinstance(obj, dict) else text
        except ValueError:
            pass
        text = text.strip()[:2000]
        if out.get("tok_per_s"):
            self.last_speed = out["tok_per_s"]
        if not text:
            return {"ok": False, "text": "The language model gave an empty answer. Try asking in a different way.", "seconds": round(time.time() - started, 2)}
        convo.append({"role": "user", "content": f'Follow-up question from the user: "{question.strip()}"'})
        convo.append({"role": "assistant", "content": json.dumps({"thought": "", "action": "answer", "cause": "none", "message": text})})
        return {"ok": True, "text": text, "seconds": round(time.time() - started, 2), "tok_per_s": out.get("tok_per_s")}

    streams = True  # step() and say() accept on_text and report the reply while it is being written

    def step(self, convo: list, view: dict, on_text=None) -> dict:
        started = time.time()
        allowed = view["allowed"]
        try:
            schema = decision_schema(allowed, bool(view.get("ready")), bool(view.get("answer_only")), view.get("causes"))
            out = self._chat(convo, schema, on_text=on_text if self.api == "ollama" else None)
        except Exception as e:
            err = _short_error(e)
            if "took too long" in err and not self.server_up(1.0):
                err = "the local model server is not running"  # Windows reports a closed port as a timeout
            d = self.fallback.step(convo, view)
            d.update({"source": "rules", "model_error": err, "seconds": round(time.time() - started, 2)})
            self.record(convo, d)
            return d
        seconds = round(time.time() - started, 2)
        if out.get("tok_per_s"):
            self.last_speed = out["tok_per_s"]
        d = parse_decision(out["text"])
        valid = bool(d) and (d["action"] in allowed or d["action"] in ("conclude", "answer"))
        if valid and d["action"] == "conclude" and d["cause"] not in CAUSES:
            valid = False
        if valid and d["action"] == "answer" and not d["message"]:
            valid = False
        if not valid:
            fb = self.fallback.step(convo, view)
            fb.update({"source": "rules", "model_error": "the model's reply was not usable: " + (out["text"] or "")[:120], "seconds": seconds})
            self.record(convo, fb)
            return fb
        convo.append({"role": "assistant", "content": json.dumps({k: d[k] for k in ("thought", "action", "cause", "message")})})
        d.update({"source": "model", "seconds": seconds, "tokens": out.get("tokens"), "tok_per_s": out.get("tok_per_s")})
        d.update({k: out.get(k) for k in ("prompt_tokens", "prompt_seconds", "gen_seconds")})
        return d


def _short_error(e: Exception) -> str:
    reason = getattr(e, "reason", None)
    text = str(reason if reason else e) or type(e).__name__
    if "refused" in text.lower() or "10061" in text:
        return "the local model server is not running"
    if "timed out" in text.lower():
        return "the local model took too long to answer"
    return text[:200]
