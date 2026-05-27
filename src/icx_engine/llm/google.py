from __future__ import annotations
import json
from icx_engine.exceptions import AuthError, ContextBuildError, RateLimited, SourceUnavailable
from icx_engine.llm.base import LLMProvider, SYSTEM_PROMPT, build_user_message, finalize, _strip_json_fencing
from icx_engine.models.config import ChannelConfig
from icx_engine.models.output import RawIssueData, IssueContext


class GeminiProvider(LLMProvider):
    def __init__(self, config: ChannelConfig):
        self._api_key = config.api_key
        self.model_name = config.model

    async def analyze(self, raw: RawIssueData) -> IssueContext:
        from google import genai
        from google.genai import types
        from google.genai.errors import ClientError, ServerError

        # Fresh client per call - avoids event-loop detachment when asyncio.run()
        # is called multiple times in the same process (e.g. icx model --add validation).
        client = genai.Client(api_key=self._api_key)
        cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.0,
        )
        try:
            response = await client.aio.models.generate_content(
                model=self.model_name,
                contents=build_user_message(raw),
                config=cfg,
            )
        except ClientError as exc:
            if getattr(exc, "code", None) == 429:
                raise RateLimited("Gemini quota exceeded. Wait briefly and retry.") from exc
            raise AuthError(
                "Gemini API key is invalid or request rejected. Re-run `icx model --add` to update it."
            ) from exc
        except ServerError as exc:
            raise SourceUnavailable(
                "Cannot connect to Gemini API. Check your network connection."
            ) from exc

        content = response.text or ""
        try:
            return finalize(IssueContext.model_validate(json.loads(_strip_json_fencing(content))), raw)
        except Exception as exc:
            raise ContextBuildError(
                "Could not parse Gemini output."
                " Run with --debug --traceback to see the raw LLM response.",
                raw_output=content,
            ) from exc
