import json
import openai
from openai import AsyncOpenAI
from icx_engine.exceptions import AuthError, ContextBuildError, RateLimited, SourceUnavailable
from icx_engine.llm.base import LLMProvider, SYSTEM_PROMPT, build_user_message, finalize, _strip_json_fencing
from icx_engine.models.config import ChannelConfig
from icx_engine.models.output import RawIssueData, IssueContext


class NIMProvider(LLMProvider):
    def __init__(self, config: ChannelConfig):
        self.client = AsyncOpenAI(
            base_url=config.base_url or "https://integrate.api.nvidia.com/v1",
            api_key=config.api_key,
        )
        self.model = config.model

    async def analyze(self, raw: RawIssueData) -> IssueContext:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_message(raw)},
                ],
                temperature=0,
            )
        except openai.AuthenticationError as exc:
            raise AuthError(
                "NIM API key is invalid or expired. Re-run `icx model --add` to update it."
            ) from exc
        except openai.RateLimitError as exc:
            raise RateLimited(
                "NIM rate limit reached. Wait briefly and retry."
            ) from exc
        except openai.APIConnectionError as exc:
            hint = (
                " NIM 405B is a reasoning model that may be unstable on the free tier. "
                "Switch to `deepseek-ai/deepseek-v3` or `meta/llama-3.1-70b-instruct` for reliable output."
                if "405b" in self.model.lower()
                else ""
            )
            raise SourceUnavailable(
                f"Cannot connect to NIM at {self.client.base_url}.{hint} "
                "Check your network and NIM base URL."
            ) from exc

        content = response.choices[0].message.content or ""
        try:
            return finalize(IssueContext.model_validate(json.loads(_strip_json_fencing(content))), raw)
        except Exception as exc:
            hint = (
                " The NIM 405B reasoning model emits internal thinking tokens before its JSON output."
                " Switch to a standard instruction-following model via `icx model --add`."
                if "405b" in self.model.lower()
                else ""
            )
            raise ContextBuildError(
                f"Could not parse structured output.{hint}"
                " Run with --debug --traceback to see the raw LLM response.",
                raw_output=content,
            ) from exc
