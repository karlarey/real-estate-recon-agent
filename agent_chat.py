"""Chat agent over the reconciliation data, backed by a local Ollama model.

The analyst asks a question in plain English; the model decides which read-only
tools (tools.py) to call, calls them, and answers grounded in the rows returned.
Every answer carries the tools it used and the rows it cited, so a claim can be
traced back to the ledger.

Degrades gracefully: if Ollama is not running (or the model is missing), the
agent says so and the rest of the app is unaffected. Nothing here writes.

    from agent_chat import chat, available
    chat("which tenants are over 30 days late?", state)

Config:
    OLLAMA_URL    default http://127.0.0.1:11434
    OLLAMA_MODEL  default llama3.1:8b
"""

import json
import os
import urllib.error
import urllib.request

import env
import tools

env.load()   # pick up OLLAMA_* from .env before reading config below

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
REQUEST_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "120"))
MAX_STEPS = 4

SYSTEM_PROMPT = (
    "You are a property-accounting assistant embedded in a reconciliation app. "
    "You answer questions about the reconciliation data for a small property "
    "portfolio.\n\n"
    "Rules you must follow:\n"
    "1. Never guess a number. Call a tool to get the data, then quote it exactly.\n"
    "2. Only pass a filter argument if the question actually asks for it. If the "
    "user does not name a property, omit property_id entirely -- do not send "
    "null, an empty string, or a placeholder.\n"
    "3. Call list_ar_exceptions with no filters to see all rent exceptions; do "
    "not narrow to one status unless the question asks for that status.\n"
    "4. If a tool returns count 0, say nothing matched the filters you used, and "
    "state which filters you applied. Never report 'none exist' when you "
    "filtered the result down to nothing.\n"
    "5. Be concise and concrete: name the unit, tenant, invoice or property and "
    "give the amount. A short bulleted list beats a paragraph.\n"
    "6. When you give a total, say which rows it covers.\n"
    "7. You are read-only. You cannot approve, reject, or change anything -- if "
    "asked to, say the reviewer must do it in the Workflow Review tab."
)


class OllamaUnavailable(RuntimeError):
    """Raised when the local model cannot be reached."""


def _post(path, payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_URL + path, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def available():
    """True when the Ollama server answers and the configured model is pulled."""
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False

    names = [m.get("name", "") for m in data.get("models", [])]
    return any(n == OLLAMA_MODEL or n.startswith(OLLAMA_MODEL.split(":")[0])
               for n in names)


def _chat_once(messages):
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "tools": tools.schemas(),
        "stream": False,
        "options": {"temperature": 0.1},
    }
    try:
        return _post("/api/chat", payload)
    except urllib.error.URLError as exc:
        raise OllamaUnavailable(str(exc))
    except Exception as exc:
        raise OllamaUnavailable("%s: %s" % (type(exc).__name__, exc))


def chat(question, state, max_steps=MAX_STEPS):
    """Answer a question about the data, calling tools as needed.

    Returns:
        {
          "answer": str,
          "tools_used": [{"tool", "arguments"}],
          "citations": [{"tool", "count", ...}],   # what each call returned
          "steps": int,
          "source": "ollama:<model>" | "unavailable",
        }
    """
    if not (question or "").strip():
        return {"answer": "Ask a question about the reconciliation data.",
                "tools_used": [], "citations": [], "steps": 0,
                "source": "unavailable"}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    used, citations = [], []

    try:
        for step in range(1, max_steps + 1):
            response = _chat_once(messages)
            message = response.get("message", {}) or {}
            calls = message.get("tool_calls") or []

            if not calls:
                answer = (message.get("content") or "").strip()
                if not answer:
                    answer = ("The model returned an empty answer. Try "
                              "rephrasing, or check that %s is pulled."
                              % OLLAMA_MODEL)
                return {
                    "answer": answer,
                    "tools_used": used,
                    "citations": citations,
                    "steps": step,
                    "source": "ollama:%s" % OLLAMA_MODEL,
                }

            # Record the assistant turn verbatim so the model sees its own calls.
            messages.append(message)

            for call in calls:
                fn = call.get("function", {}) or {}
                name = fn.get("name", "")
                arguments = fn.get("arguments") or {}
                if isinstance(arguments, str):          # some builds send a string
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {}

                result = tools.call_tool(name, arguments, state)
                used.append({"tool": name, "arguments": arguments})
                citations.append({
                    "tool": name,
                    "arguments": arguments,
                    "count": result.get("count"),
                    "total": result.get("total_variance") or result.get("total_value")
                             or result.get("total_billed"),
                })
                messages.append({
                    "role": "tool",
                    "name": name,
                    "content": json.dumps(result)[:8000],
                })

        return {
            "answer": ("Stopped after %d tool calls without reaching an answer. "
                       "Try a narrower question." % max_steps),
            "tools_used": used,
            "citations": citations,
            "steps": max_steps,
            "source": "ollama:%s" % OLLAMA_MODEL,
        }

    except OllamaUnavailable as exc:
        return {
            "answer": ("The assistant is unavailable: could not reach Ollama at "
                       "%s (%s). Start it with `ollama serve` and pull the model "
                       "with `ollama pull %s`. Everything else in the app still "
                       "works." % (OLLAMA_URL, exc, OLLAMA_MODEL)),
            "tools_used": [],
            "citations": [],
            "steps": 0,
            "source": "unavailable",
        }
