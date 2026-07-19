"""
Local RAG Voice Agent Server
-----------------------------
Serves the agent UI (agent.html) and two APIs:

  GET  /api/run    -> run the pipeline on demand with explicit params
                       (kept for compatibility / manual scenario testing)
  POST /api/ask     -> ask a question; answered by Groq, grounded in the
                       latest cached pipeline result + recent real news
                       headlines (a lightweight RAG: the "retrieval" is
                       just handing the model our own structured JSON and
                       real signals as context, no vector DB needed since
                       it's a few KB of data, not a document corpus)

A background thread refreshes the cached pipeline result + news every
REFRESH_INTERVAL seconds, so /api/ask answers instantly instead of waiting
on GDELT/Yahoo/AIS fetches per question.

    python server.py
    python server.py --port 9000

Needs a Groq API key. Put it in a local .env file (gitignored, never
committed) next to this script:

    echo "GROQ_API_KEY=your_key" > .env

Still stdlib-only — the Groq REST API is called directly via urllib.request,
no SDK dependency.
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from data_sources import generate_news_signals
from models import to_dict
from orchestrator import run_pipeline

AGENT_HTML_PATH = Path(__file__).with_name("agent.html")
ENV_PATH = Path(__file__).with_name(".env")
CHAT_LOG_PATH = Path(__file__).with_name("chat_history.jsonl")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
REFRESH_INTERVAL_SECONDS = 90
CHAT_HISTORY_TURNS = 10  # how many past exchanges to feed back to the model as context


def _load_env_file(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


_dotenv = _load_env_file(ENV_PATH)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") or _dotenv.get("GROQ_API_KEY")


# ---------------------------------------------------------------------------
# Background-refreshed cache: the "knowledge" the agent answers questions
# from. Refreshed on a timer rather than per-question so answering feels
# instant (a fresh run involves several real network calls and can take
# 10-40s, which would make every question wait that long otherwise).
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache = {"result": None, "news": [], "updated_at": None, "error": None}


def _refresh_cache():
    try:
        rec = run_pipeline()
        news = generate_news_signals()
    except Exception as exc:
        print(f"[agent] cache refresh failed: {exc}", file=sys.stderr)
        with _cache_lock:
            _cache["error"] = str(exc)
        return
    with _cache_lock:
        _cache["result"] = rec
        _cache["news"] = news
        _cache["updated_at"] = time.time()
        _cache["error"] = None


def _background_refresh_loop():
    while True:
        time.sleep(REFRESH_INTERVAL_SECONDS)
        _refresh_cache()


def _build_context() -> dict | None:
    with _cache_lock:
        rec, news, updated_at = _cache["result"], _cache["news"], _cache["updated_at"]
    if rec is None:
        return None
    return {
        "pipeline_result": to_dict(rec),
        "recent_news": [
            {"headline": n.headline, "source": n.source, "sentiment": n.sentiment}
            for n in news[:8]
        ],
        "data_as_of_unix": updated_at,
    }


# ---------------------------------------------------------------------------
# Persistent chat memory: every exchange is appended to a local JSONL file
# (a plain text file, one JSON object per line) so the agent has continuity
# across questions — and across server restarts, unlike an in-memory list.
# Only the last CHAT_HISTORY_TURNS exchanges are fed back to the model per
# question, so the prompt doesn't grow unbounded even though the file does.
# ---------------------------------------------------------------------------
_chat_log_lock = threading.Lock()


def _append_chat_log(question: str, answer: str):
    entry = {"ts": time.time(), "question": question, "answer": answer}
    with _chat_log_lock:
        with CHAT_LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")


def _load_recent_chat_history(n: int) -> list:
    if not CHAT_LOG_PATH.exists():
        return []
    with _chat_log_lock:
        lines = CHAT_LOG_PATH.read_text().splitlines()
    history = []
    for line in lines[-n:]:
        try:
            history.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return history


SYSTEM_PROMPT = """You are a supply-chain risk analyst assistant for a live pipeline monitoring \
Strait of Hormuz disruption risk to India's oil imports. Answer the user's question using ONLY \
the JSON data provided below, from the most recent pipeline run.

Data provenance, be honest about this if asked: news headlines, market prices (Brent/WTI), and \
tanker/AIS counts for Hormuz routes are REAL live data. Refinery and SPR reserve capacities are \
real published figures but static (not live). SPR current fill levels, non-Hormuz route AIS data, \
and exact supplier contract terms are illustrative placeholders, not real figures.

Be concise, conversational (this answer will be read aloud via text-to-speech), and cite specific \
numbers from the data. If the question isn't covered by the data, say so rather than inventing an \
answer. Prior turns in this conversation are included as message history — use them for follow-up \
questions (e.g. "what about the second one") but always defer to the DATA below for facts/numbers.

DATA:
{context_json}"""


def _ask_groq(question: str, context: dict, history: list) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not configured — add it to .env and restart the server.")

    system_prompt = SYSTEM_PROMPT.format(context_json=json.dumps(context, default=str))
    messages = [{"role": "system", "content": system_prompt}]
    # Past exchanges as real conversation turns (not stuffed into the prompt
    # text) — this is what lets the model resolve things like "what about
    # the second one?" against what was actually said before.
    for turn in history:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})
    messages.append({"role": "user", "content": question})

    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 600,
    }
    req = urllib.request.Request(
        GROQ_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
            # Groq's API sits behind Cloudflare, which blocks urllib's default
            # User-Agent as a bot signature (HTTP 403 / Cloudflare error 1010) —
            # a browser-like one clears it. Confirmed by testing both directly.
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"]


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "HormuzAgent/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[server] {self.address_string()} - {fmt % args}\n")

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- GET ---

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send_agent_page()
        elif parsed.path == "/api/run":
            self._send_run(urllib.parse.parse_qs(parsed.query))
        elif parsed.path == "/api/status":
            self._send_status()
        else:
            self._send_json(404, {"error": "not found"})

    def _send_agent_page(self):
        try:
            body = AGENT_HTML_PATH.read_bytes()
        except FileNotFoundError:
            self._send_json(500, {"error": "agent.html missing"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_run(self, query: dict):
        def first(name, default, cast):
            if query.get(name) and query[name][0] != "":
                try:
                    return cast(query[name][0])
                except (ValueError, TypeError):
                    return default
            return default

        scenario = first("scenario", "Strait of Hormuz flow disruption", str)
        disruption = first("disruption", 0.5, float)
        region = first("region", "Hormuz", str)
        seed = first("seed", None, int)

        try:
            rec = run_pipeline(
                scenario_name=scenario,
                disruption_pct=disruption,
                region_of_concern=region,
                seed=seed,
            )
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, to_dict(rec))

    def _send_status(self):
        with _cache_lock:
            ready = _cache["result"] is not None
            updated_at = _cache["updated_at"]
            error = _cache["error"]
        self._send_json(200, {
            "ready": ready,
            "data_as_of_unix": updated_at,
            "last_error": error,
            "groq_configured": bool(GROQ_API_KEY),
        })

    # --- POST ---

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/ask":
            self._handle_ask()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_ask(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            question = (body.get("question") or "").strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return

        if not question:
            self._send_json(400, {"error": "'question' is required"})
            return

        context = _build_context()
        if context is None:
            self._send_json(503, {"error": "Pipeline data isn't ready yet — try again in a few seconds."})
            return

        history = _load_recent_chat_history(CHAT_HISTORY_TURNS)
        try:
            answer = _ask_groq(question, context, history)
        except urllib.error.HTTPError as exc:
            self._send_json(502, {"error": f"Groq API error: HTTP {exc.code}"})
            return
        except Exception as exc:
            self._send_json(502, {"error": f"Groq request failed: {exc}"})
            return

        _append_chat_log(question, answer)
        self._send_json(200, {"answer": answer, "data_as_of_unix": context["data_as_of_unix"]})


def main():
    parser = argparse.ArgumentParser(description="Local RAG voice agent for the Hormuz pipeline")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not GROQ_API_KEY:
        print("[agent] WARNING: GROQ_API_KEY not set (checked .env and the environment). "
              "The agent will run but /api/ask will fail until it's configured.", file=sys.stderr)

    print("[agent] Warming up the data cache (first pipeline run — this can take up to ~40s)...")
    _refresh_cache()
    threading.Thread(target=_background_refresh_loop, daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), AgentHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Hormuz agent running at {url} (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
