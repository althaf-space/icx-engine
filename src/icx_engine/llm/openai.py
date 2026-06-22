import json
import openai
from openai import AsyncOpenAI
from icx_engine.exceptions import AuthError, ContextBuildError, RateLimited, SourceUnavailable
from icx_engine.llm.base import LLMProvider, SYSTEM_PROMPT, build_user_message, finalize, _strip_json_fencing
from icx_engine.models.config import ChannelConfig
from icx_engine.models.output import RawIssueData, IssueContext


class OpenAIProvider(LLMProvider):
    def __init__(self, config: ChannelConfig):
        self.client = AsyncOpenAI(api_key=config.api_key)
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
                "OpenAI API key is invalid or expired. Re-run `icx model --add` to update it."
            ) from exc
        except openai.RateLimitError as exc:
            raise RateLimited("OpenAI rate limit reached. Wait briefly and retry.") from exc
        except openai.APIConnectionError as exc:
            raise SourceUnavailable(
                "Cannot connect to OpenAI API. Check your network connection."
            ) from exc
        except openai.APIStatusError as exc:
            raise SourceUnavailable(
                f"OpenAI API returned an error (status {exc.status_code}). Try again later."
            ) from exc

        content = response.choices[0].message.content if response.choices else ""
        try:
            return finalize(IssueContext.model_validate(json.loads(_strip_json_fencing(content))), raw)
        except Exception as exc:
            raise ContextBuildError(
                "Could not parse structured output."
                " Run with --debug --traceback to see the raw LLM response.",
                raw_output=content,
            ) from exc
