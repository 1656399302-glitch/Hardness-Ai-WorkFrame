"""
Agent implementation — the core while loop with tool use.
Uses OpenAI-compatible chat completions API with function calling.
"""
from __future__ import annotations

import json
import time
import logging
from urllib import error as urllib_error
from urllib import request as urllib_request

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

import config
import tools
import context
from runtime_state import append_event, write_state

log = logging.getLogger("harness")

# ---------------------------------------------------------------------------
# LLM client (singleton)
# ---------------------------------------------------------------------------

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.API_KEY,
            base_url=config.BASE_URL,
            timeout=float(config.API_REQUEST_TIMEOUT_SECONDS),
            max_retries=0,
        )
    return _client


def extract_primary_choice(response) -> object:
    """Return the first choice from a chat completion response or raise a useful error."""
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError(f"response did not include choices ({_response_preview(response)})")

    choice = choices[0]
    if choice is None:
        raise ValueError(f"response returned an empty first choice ({_response_preview(response)})")

    if getattr(choice, "message", None) is None:
        raise ValueError(f"response choice did not include a message ({_response_preview(response)})")

    return choice


def llm_call_simple(messages: list[dict]) -> str:
    """Simple LLM call without tools — used for summarization."""
    resp = get_client().chat.completions.create(
        model=config.MODEL,
        messages=messages,
        max_tokens=10000,
    )
    try:
        choice = extract_primary_choice(resp)
    except ValueError as e:
        log.error(f"[summarizer] Invalid API response: {e}")
        return ""
    return choice.message.content or ""


def _is_fatal_api_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            AuthenticationError,
            PermissionDeniedError,
            BadRequestError,
            NotFoundError,
            ConflictError,
            UnprocessableEntityError,
        ),
    )


def _is_connectivity_error(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "connection error",
            "connection reset",
            "connection refused",
            "network is unreachable",
            "temporary failure in name resolution",
            "name or service not known",
            "server disconnected",
            "remote end closed",
            "nodename nor servname provided",
        )
    )


def _is_retryable_api_error(exc: Exception) -> bool:
    if _is_connectivity_error(exc):
        return True
    if isinstance(exc, (RateLimitError, InternalServerError)):
        return True
    if isinstance(exc, APIStatusError):
        status_code = getattr(exc, "status_code", None)
        return status_code is None or status_code >= 500 or status_code in {408, 409, 425, 429}
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "rate limit",
            "too many requests",
            "service unavailable",
            "temporarily unavailable",
            "bad gateway",
            "gateway timeout",
            "server error",
            "overloaded",
        )
    )


def _retry_delay_seconds(attempt: int) -> int:
    base = max(config.API_RETRY_BACKOFF_SECONDS, 1)
    ceiling = max(config.API_RETRY_MAX_BACKOFF_SECONDS, base)
    return min(base * (2 ** max(attempt - 1, 0)), ceiling)


def _probe_api_base_url() -> tuple[bool, str]:
    try:
        request = urllib_request.Request(
            config.BASE_URL,
            headers={"User-Agent": "Harness/1.0"},
            method="HEAD",
        )
        with urllib_request.urlopen(request, timeout=min(config.API_RECOVERY_POLL_SECONDS, 5)) as response:
            status = getattr(response, "status", 200)
            return True, f"HTTP {status}"
    except urllib_error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def _wait_for_api_recovery(agent_name: str, iteration: int, error: Exception, outage_started_at: float) -> bool:
    append_event(
        "api_outage",
        "Transient API connectivity issue detected",
        agent=agent_name,
        iteration=iteration,
        error=str(error),
    )

    while True:
        elapsed = int(time.time() - outage_started_at)
        if config.API_MAX_RECOVERY_WAIT_SECONDS > 0 and elapsed >= config.API_MAX_RECOVERY_WAIT_SECONDS:
            return False

        reachable, detail = _probe_api_base_url()
        if reachable:
            append_event(
                "api_recovered",
                "API connectivity recovered",
                agent=agent_name,
                iteration=iteration,
                elapsed_seconds=elapsed,
                detail=detail,
            )
            log.warning(
                f"[{agent_name}] API reachable again after {elapsed}s ({detail}). Retrying iteration {iteration}."
            )
            write_state(
                active_agent=agent_name,
                message=f"{agent_name} API recovered at iteration {iteration} after {elapsed}s",
            )
            return True

        wait_seconds = max(config.API_RECOVERY_POLL_SECONDS, 1)
        log.warning(
            f"[{agent_name}] Waiting for API recovery at iteration {iteration} "
            f"(offline for {elapsed}s, probe={detail!r}). Retrying in {wait_seconds}s."
        )
        write_state(
            active_agent=agent_name,
            message=f"{agent_name} waiting for network recovery at iteration {iteration} ({elapsed}s)",
        )
        time.sleep(wait_seconds)


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------

class Agent:
    """
    A single agent with a system prompt and tool access.

    This is the 'managed agent loop' from the architecture:
    - while loop with llm.call(prompt)
    - tool execution
    - context lifecycle (compaction / reset)

    Skills are handled via progressive disclosure:
    - Level 1: skill catalog (name + description) is baked into system_prompt
    - Level 2: agent decides to read_skill_file("skills/.../SKILL.md") on its own
    - Level 3: SKILL.md references sub-files, agent reads those too
    No external code decides which skills to load — the agent does.
    """

    def __init__(self, name: str, system_prompt: str, use_tools: bool = True,
                 extra_tool_schemas: list[dict] | None = None):
        self.name = name
        self.system_prompt = system_prompt
        self.use_tools = use_tools
        self.extra_tool_schemas = extra_tool_schemas or []
        self.last_run_success = False
        self.last_stop_reason = "not_started"
        self.last_iterations = 0
        self.last_tool_uses: list[dict] = []

    def run(self, task: str) -> str:
        """
        Execute the agent loop until the model stops calling tools
        or we hit the iteration limit.

        Returns the final assistant text response.
        """
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]

        client = get_client()
        consecutive_errors = 0
        last_text = ""
        self.last_run_success = False
        self.last_stop_reason = "in_progress"
        self.last_iterations = 0
        self.last_tool_uses = []
        write_state(active_agent=self.name, message=f"{self.name} started")
        should_abort = False

        for iteration in range(1, config.MAX_AGENT_ITERATIONS + 1):
            self.last_iterations = iteration
            write_state(active_agent=self.name, message=f"{self.name} iteration {iteration}")
            # --- Context lifecycle check ---
            token_count = context.count_tokens(messages)
            log.info(f"[{self.name}] iteration={iteration}  tokens≈{token_count}")

            if token_count > config.RESET_THRESHOLD or context.detect_anxiety(messages):
                reason = "anxiety detected" if token_count <= config.RESET_THRESHOLD else f"tokens {token_count} > threshold"
                log.warning(f"[{self.name}] Context reset triggered ({reason}). Writing checkpoint...")
                checkpoint = context.create_checkpoint(messages, llm_call_simple)
                messages = context.restore_from_checkpoint(checkpoint, self.system_prompt)
            elif token_count > config.COMPRESS_THRESHOLD:
                log.info(f"[{self.name}] Compacting context (role={self.name})...")
                messages = context.compact_messages(messages, llm_call_simple, role=self.name)

            # --- LLM call ---
            kwargs = dict(
                model=config.MODEL,
                messages=messages,
                max_tokens=32768,
            )
            if self.use_tools:
                kwargs["tools"] = tools.TOOL_SCHEMAS + self.extra_tool_schemas
                kwargs["tool_choice"] = "auto"

            response = None
            retryable_attempt = 0
            outage_started_at: float | None = None

            while response is None:
                try:
                    response = client.chat.completions.create(**kwargs)
                except Exception as e:
                    if _is_fatal_api_error(e):
                        self.last_stop_reason = "fatal_api_error"
                        log.error(f"[{self.name}] Fatal API error: {e}")
                        write_state(active_agent=self.name, message=f"{self.name} hit a fatal API error")
                        should_abort = True
                        break

                    if _is_retryable_api_error(e):
                        retryable_attempt += 1
                        if outage_started_at is None:
                            outage_started_at = time.time()

                        if _is_connectivity_error(e):
                            log.error(
                                f"[{self.name}] API connectivity issue at iteration {iteration}: {e}"
                            )
                            recovered = _wait_for_api_recovery(
                                self.name,
                                iteration,
                                e,
                                outage_started_at,
                            )
                            if not recovered:
                                self.last_stop_reason = "api_recovery_timeout"
                                log.error(
                                    f"[{self.name}] API recovery wait exceeded "
                                    f"{config.API_MAX_RECOVERY_WAIT_SECONDS}s; aborting."
                                )
                                should_abort = True
                                break
                            continue

                        delay = _retry_delay_seconds(retryable_attempt)
                        log.error(
                            f"[{self.name}] Transient API error at iteration {iteration}: {e}. "
                            f"Retrying same iteration in {delay}s."
                        )
                        write_state(
                            active_agent=self.name,
                            message=f"{self.name} retrying iteration {iteration} after transient API error",
                        )
                        append_event(
                            "api_retry",
                            "Transient API error",
                            agent=self.name,
                            iteration=iteration,
                            error=str(e),
                            delay_seconds=delay,
                        )
                        time.sleep(delay)
                        continue

                    log.error(f"[{self.name}] API error: {e}")
                    consecutive_errors += 1
                    if consecutive_errors >= config.MAX_TOOL_ERRORS:
                        self.last_stop_reason = "api_error"
                        log.error(f"[{self.name}] Too many API errors, aborting.")
                        should_abort = True
                        break
                    time.sleep(2 ** consecutive_errors)

                if response is not None and outage_started_at is not None:
                    recovered_after = int(time.time() - outage_started_at)
                    log.info(
                        f"[{self.name}] API call resumed successfully after {recovered_after}s. "
                        f"Continuing iteration {iteration} without incrementing counters."
                    )
                    write_state(
                        active_agent=self.name,
                        message=f"{self.name} resumed iteration {iteration} after API recovery",
                    )

            if should_abort:
                break

            try:
                choice = extract_primary_choice(response)
            except ValueError as e:
                log.error(f"[{self.name}] Invalid API response: {e}")
                consecutive_errors += 1
                if consecutive_errors >= config.MAX_TOOL_ERRORS:
                    self.last_stop_reason = "invalid_response"
                    log.error(f"[{self.name}] Too many invalid API responses, aborting.")
                    break
                time.sleep(2 ** consecutive_errors)
                continue

            consecutive_errors = 0
            msg = choice.message

            # --- Append assistant message to history ---
            assistant_msg = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg)

            # --- If model produced text, capture it ---
            if msg.content:
                last_text = msg.content
                log.info(f"[{self.name}] assistant: {msg.content[:200]}...")

            # --- If no tool calls, we're done ---
            if not msg.tool_calls:
                self.last_run_success = True
                self.last_stop_reason = "completed"
                write_state(active_agent=self.name, message=f"{self.name} completed")
                log.info(f"[{self.name}] Finished (no more tool calls).")
                break

            # --- Execute tool calls ---
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    log.warning(f"[{self.name}] Bad JSON in tool call {fn_name}: {tc.function.arguments[:200]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"[error] Invalid JSON arguments: {tc.function.arguments[:200]}",
                    })
                    continue

                log.info(f"[{self.name}] tool: {fn_name}({_truncate(str(fn_args), 120)})")
                result = tools.execute_tool(fn_name, fn_args)
                log.debug(f"[{self.name}] tool result: {_truncate(result, 200)}")
                self.last_tool_uses.append({
                    "name": fn_name,
                    "arguments": fn_args,
                    "result": result,
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # --- Check finish reason ---
            if choice.finish_reason == "stop":
                self.last_run_success = True
                self.last_stop_reason = "stop"
                write_state(active_agent=self.name, message=f"{self.name} stopped cleanly")
                log.info(f"[{self.name}] Finished (stop).")
                break

            if choice.finish_reason == "length":
                log.warning(f"[{self.name}] Output truncated (max_tokens hit). Asking model to retry with smaller chunks.")
                messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] Your last response was cut off because it exceeded the token limit. "
                        "The tool call was NOT executed. "
                        "Please retry, but split large files into smaller parts:\n"
                        "1. Write the first half of the file with write_file\n"
                        "2. Then write the second half as a separate file or append\n"
                        "Or simplify the implementation to fit in one response."
                    ),
                })

        else:
            self.last_stop_reason = "max_iterations"
            log.warning(f"[{self.name}] Hit max iterations ({config.MAX_AGENT_ITERATIONS}).")

        write_state(active_agent="", message=f"{self.name} ended with {self.last_stop_reason}")
        return last_text


def _truncate(s: str, n: int) -> str:
    return s[:n] + "..." if len(s) > n else s


def _response_preview(response) -> str:
    try:
        if hasattr(response, "model_dump_json"):
            return _truncate(response.model_dump_json(), 500)
    except Exception:
        pass
    return _truncate(repr(response), 500)
