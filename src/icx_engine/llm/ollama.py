import json
import openai
from openai import AsyncOpenAI
from icx_engine.exceptions import AuthError, ContextBuildError, RateLimited, SourceUnavailable
from icx_engine.llm.base import LLMProvider, SYSTEM_PROMPT, build_user_message, finalize, _strip_json_fencing
from icx_engine.models.config import ChannelConfig
from icx_engine.models.output import RawIssueData, IssueContext


class OllamaProvider(LLMProvider):
    def __init__(self, config: ChannelConfig):
        self.client = AsyncOpenAI(
            base_url=config.base_url or "http://localhost:11434/v1",
            api_key="ollama",
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
                "Ollama rejected the request. Check your Ollama server configuration."
            ) from exc
        except openai.RateLimitError as exc:
            raise RateLimited("Ollama rate limit reached. Wait briefly and retry.") from exc
        except openai.APIConnectionError as exc:
            raise SourceUnavailable(
                f"Cannot connect to Ollama at {self.client.base_url}. "
                "Ensure Ollama is running: `ollama serve`."
            ) from exc
        except openai.APIStatusError as exc:
            raise SourceUnavailable(
                f"Ollama returned an error (status {exc.status_code})."
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
