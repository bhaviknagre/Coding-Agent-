"""Real model services, through their OpenAI-format address. The launcher alone talks to them, over TLS, with the key
in a header. The reply goes back unchanged: Gemini needs its own fields (thought signatures) returned as they were."""
import http.client
import json
import random
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from agent.mask import mask
from agent.model import Message, ModelError, Reply

GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL = "gemini-3.8-flash"
# When Google says the default model is busy (503), these lighter ones often still answer. Only for the default.
GEMINI_BUSY = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
CALL_SECONDS = 180  # for one whole reply, not only each piece of it
CALLS_PER_MINUTE = 5  # until the account's own limits are known; the service's own answer always wins
NEXT = {
    429: ". Next: wait a minute, or try tomorrow: the free tier allows only some requests a minute and a day."
    " Or another model: --model gemini:gemini-3.5-flash-lite",
    503: ". Next: try again in a few minutes, or --model gemini:gemini-3.5-flash-lite",
}
TRIES = 4  # the first and three more, only for "too many requests" and the service's own faults


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect could send the key somewhere else."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Limiter:
    """Spaces calls out, for all tasks of this program."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.gap, self.clock, self.sleep, self.next = 60 / per_minute, clock, sleep, 0.0
        self.lock = threading.Lock()

    def wait(self, at_least: float = 0) -> None:
        with self.lock:
            now = self.clock()
            start = max(now, self.next, now + at_least)
            if start > now:
                self.sleep(start - now)
            self.next = start + self.gap


class OpenAIFormat:
    def __init__(
        self, key: str, model: str = GEMINI_MODEL, address: str = GEMINI, name: str = "gemini",
        limiter: Limiter | None = None, sleep: Callable[[float], None] = time.sleep,
        busy: Sequence[str] = (), note: Callable[[str], None] = lambda text: None,
    ) -> None:
        self.busy, self.note, self.service = list(busy), note, name
        u = urlsplit(address)
        local = u.scheme == "http" and u.hostname == "127.0.0.1" and not u.username and not u.fragment  # tests only
        if not (u.scheme == "https" and u.hostname and not u.username) and not local:
            raise ModelError("the model service address must use https")
        self.key, self.model, self.address, self.name = key, model, address, f"{name}:{model}"
        self.limiter, self.sleep = limiter or Limiter(CALLS_PER_MINUTE), sleep
        context = ssl.create_default_context()
        self.opener = urllib.request.build_opener(NoRedirects, urllib.request.HTTPSHandler(context=context))

    def call(self, messages: Sequence[Message], tools: Sequence[Message]) -> Reply:
        while True:
            try:
                return self._call(messages, tools)
            except ModelError as e:
                if not (self.busy and e.busy):
                    raise
                old, self.model = self.model, self.busy.pop(0)
                self.name = f"{self.service}:{self.model}"
                self.note(f"{old} cannot answer now ({e.code}); using {self.model} from now on")

    def _call(self, messages: Sequence[Message], tools: Sequence[Message]) -> Reply:
        body = json.dumps({"model": self.model, "messages": list(messages), "tools": list(tools)}).encode()
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        wait = 0.0
        for attempt in range(TRIES):
            self.limiter.wait(wait)
            request = urllib.request.Request(self.address, body, headers, method="POST")  # noqa: S310 - checked above
            try:
                with self.opener.open(request, timeout=60) as r:
                    data = json.loads(self._read(r))
                break
            except urllib.error.HTTPError as e:
                text = self._said(e)
                if e.code in (401, 403):
                    raise ModelError(f"the key was refused ({e.code}). Next: agent key set gemini") from None
                if e.code not in (429, 500, 502, 503, 504) or attempt == TRIES - 1:
                    error = ModelError(f"the model service said {e.code}: {text}" + NEXT.get(e.code, ""))
                    error.code = e.code
                    # Busy, or this model's free quota used up: another model has its own quota.
                    error.busy = e.code in (429, 500, 502, 503, 504)
                    raise error from None
                wait = self._retry_after(e, attempt)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
                if attempt == TRIES - 1:
                    raise ModelError(f"the model service could not be reached: {type(e).__name__}") from None
                wait = 2.0 * 2**attempt
            except (ValueError, RecursionError):
                raise ModelError("the model service sent something that is not JSON") from None
        try:
            choice = data["choices"][0]
            message, finish = choice["message"], choice.get("finish_reason")
            tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
            if not isinstance(message, dict):
                raise TypeError
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError):
            raise ModelError("the model service sent a reply without a message") from None
        message.setdefault("role", "assistant")
        return Reply(message, cut=finish == "length", tokens=tokens)

    def _read(self, r: Any) -> bytes:
        """The whole reply within CALL_SECONDS: a service that sends one byte at a time must not hold us."""
        end, data = time.monotonic() + CALL_SECONDS, b""
        while piece := r.read1(65536):
            data += piece
            if len(data) > 5_000_000:
                raise ValueError
            if time.monotonic() > end:
                raise TimeoutError
        return data

    def _said(self, e: urllib.error.HTTPError) -> str:
        try:
            text = e.read(2000).decode("utf-8", "replace")
        except OSError:
            text = ""
        finally:
            e.close()
        try:  # Google's answer is JSON; its message is what a person needs
            found = json.loads(text)
            found = found[0] if isinstance(found, list) and found else found
            text = str(found["error"]["message"])
        except (ValueError, KeyError, TypeError, IndexError, RecursionError):
            pass
        text = mask(" ".join(text.split()), (self.key,))[:300]
        # A piece of the key, cut off at the edge, would not be found by the mask.
        if any(self.key[i : i + 8] in text for i in range(len(self.key) - 7)):
            return "(not shown: it held part of the key)"
        return text

    @staticmethod
    def _retry_after(e: urllib.error.HTTPError, attempt: int) -> float:
        try:
            given = float(e.headers.get("Retry-After") or "")
        except ValueError:
            given = 0.0
        return float(min(120.0, max(given, 5.0 * 2**attempt + random.uniform(0, 1))))  # noqa: S311
