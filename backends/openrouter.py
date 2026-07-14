"""OpenRouter(OpenAI 호환 REST API) 기반 LLM 래퍼 — local_llm.LocalLLM과 동일한 인터페이스.

config.yaml의 openrouter.enabled가 false인 동안은 어디서도 호출되지 않는다.
"""
import json
import os
import requests

from backends.json_repair import try_repair_truncated_json


class OpenRouterLLM:
    def __init__(self, model: str, api_key: str = "",
                 base_url: str = "https://openrouter.ai/api/v1", temperature: float = 0.2):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature

    def generate(self, prompt: str, json_mode: bool = False, system: str = None, num_predict: int = 512) -> str | dict:
        """프롬프트를 보내고 응답을 받는다. (인터페이스는 LocalLLM.generate와 동일)"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": num_predict,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        resp = self._post_chat(payload)
        if not resp.ok and json_mode and resp.status_code == 400 and "response_format" in payload:
            # Some OpenRouter providers reject response_format even when the model can return JSON.
            # Retry without strict JSON mode and parse the text ourselves below.
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            resp = self._post_chat(fallback_payload)

        if not resp.ok:
            raise self._http_error(resp)
        body = resp.json()
        choice = body["choices"][0]
        text = choice["message"]["content"]
        if text is None:
            reason = choice.get("finish_reason", "unknown")
            raise ValueError(f"OpenRouter가 빈 응답을 반환했습니다 (finish_reason={reason}): {json.dumps(body)[:500]}")

        if json_mode:
            return self._parse_json(text)
        return text

    def _post_chat(self, payload: dict):
        return requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=300,
        )

    @staticmethod
    def _http_error(resp):
        detail = resp.text.strip()
        if len(detail) > 1000:
            detail = detail[:1000] + "..."
        return requests.HTTPError(
            f"OpenRouter HTTP {resp.status_code}: {detail}",
            response=resp,
        )

    @staticmethod
    def _parse_json(text: str):
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            repaired = try_repair_truncated_json(text)
            if repaired is not None:
                return repaired
            raise ValueError(f"OpenRouter가 유효한 JSON을 반환하지 않았습니다: {text[:300]}") from e

    def health_check(self) -> bool:
        if not self.api_key:
            return False
        try:
            r = requests.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=5,
            )
            return r.status_code == 200
        except requests.RequestException:
            return False
