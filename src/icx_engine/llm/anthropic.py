import json
import anthropic
from anthropic import AsyncAnthropic
from icx_engine.exceptions import AuthError, ContextBuildError, RateLimited, SourceUnavailable
from icx_engine.llm.base import LLMProvider, SYSTEM_PROMPT, build_user_message, finalize, _strip_json_fencing
from icx_engine.models.config import ChannelConfig
from icx_engine.models.output import RawIssueData, IssueContext


class AnthropicProvider(LLMProvider):
    def __init__(self, config: ChannelConfig):
        self.client = AsyncAnthropic(api_key=config.api_key)
        self.model = config.model

    async def analyze(self, raw: RawIssueData) -> IssueContext:
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_message(raw)}],
            )
        except anthropic.AuthenticationError as exc:
            raise AuthError(
                "Anthropic API key is invalid or expired. Re-run `icx model --add` to update it."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise RateLimited("Anthropic rate limit reached. Wait briefly and retry.") from exc
        except anthropic.APIConnectionError as exc:
            raise SourceUnavailable(
                "Cannot connect to Anthropic API. Check your network connection."
            ) from exc

        content = response.content[0].text if response.content else ""
        try:
            return finalize(IssueContext.model_validate(json.loads(_strip_json_fencing(content))), raw)
        except Exception as exc:
            raise ContextBuildError(
                "Could not parse structured output."
                " Run with --debug --traceback to see the raw LLM response.",
                raw_output=content,
            ) from exc
