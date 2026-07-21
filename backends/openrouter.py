"""OpenRouter(OpenAI 호환 REST API) 기반 LLM 래퍼 — local_llm.LocalLLM과 동일한 인터페이스.

config.yaml의 openrouter.enabled가 false인 동안은 어디서도 호출되지 않는다.

프롬프트 캐싱: harness/agentic_loop.py는 매 스텝마다 거의 그대로인 대용량
system 프롬프트(작업/프로젝트 지침)를 반복 전송하고 history만 바뀐다.
system을 cache_control이 붙은 content-block 배열로 보내면, Anthropic
계열 모델(anthropic/*)에서 OpenRouter가 이를 그대로 Anthropic API로
전달해 반복되는 system 프리픽스가 캐시되어 비용이 크게 준다 — 캐싱이
없으면 매 스텝 전체 system을 풀프라이스로 다시 처리하므로, 스텝 수에
비례해 비용이 기하급수적으로 늘어난다.
"""
import json
import os
import requests

from backends.json_repair import try_repair_truncated_json

# system 프롬프트 캐싱을 시도할 최소 길이(대략적인 토큰 추정치 기준).
# Anthropic 계열 모델의 최소 캐시 가능 프리픽스는 모델별로 1024~4096 토큰이라
# 너무 짧은 system은 캐시 마커를 붙여도 조용히 캐시되지 않는다. 대략
# 1자=~0.5토큰으로 보수적으로 잡아 이 문턱 미만이면 캐시 마커를 생략한다.
_CACHE_MIN_CHARS = 2000
# anthropic/ 계열이 아닌 모델(OpenAI, Google 등)에 Anthropic 전용
# cache_control 블록을 보내면 provider가 거부할 수 있어 접두어로 판별한다.
_CACHE_CONTROL_MODEL_PREFIXES = ("anthropic/",)

# OpenRouter 대시보드의 App 컬럼에 표시될 이름. HTTP-Referer/X-Title 헤더가 없으면
# 요청 출처가 "Unknown"으로만 표시되고 이름/아이콘이 뜨지 않는다(OpenRouter API 문서
# 기준 App attribution 헤더 — https://openrouter.ai/docs 참고, 실측으로도 확인함:
# 이 헤더를 보내는 다른 클라이언트는 App 컬럼에 이름+아이콘이 뜨고, 안 보내는 요청은
# Unknown으로 뜬다). 과금/캐싱 등 실제 동작에는 영향 없이 대시보드 식별용이다.
_APP_TITLE = "Ubisam-HJ-Agent"
_APP_REFERER = "https://github.com/ubisam-research/com.ubisam.boilerplate.research.ai.agent"


class OpenRouterLLM:
    def __init__(self, model: str, api_key: str = "",
                 base_url: str = "https://openrouter.ai/api/v1", temperature: float = 0.2,
                 cache_system: bool = True):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.cache_system = cache_system
        # 이 인스턴스로 수행된 모든 generate() 호출의 usage 누적치. agentic_loop는
        # 매 스텝 같은 인스턴스로 여러 번 generate()를 호출하므로(agent.py가 작업
        # 1건당 OpenRouterLLM을 새로 만듦), 인스턴스 수명 = 작업 1건 수명과 같아
        # 여기 누적된 값이 곧 "이 작업에서 캐싱으로 절감한 총량"이 된다.
        # generate()의 반환값(str|dict) 시그니처는 다른 호출부(agent.py, router.py)와
        # 호환을 위해 그대로 두고, 캐시 통계는 cache_stats()로 별도 조회한다.
        self._usage_totals = {
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    def _supports_cache_control(self) -> bool:
        return self.cache_system and self.model.startswith(_CACHE_CONTROL_MODEL_PREFIXES)

    def generate(self, prompt: str, json_mode: bool = False, system: str = None, num_predict: int = 512) -> str | dict:
        """프롬프트를 보내고 응답을 받는다. (인터페이스는 LocalLLM.generate와 동일)"""
        messages = []
        if system:
            if self._supports_cache_control() and len(system) >= _CACHE_MIN_CHARS:
                messages.append({
                    "role": "system",
                    "content": [{
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                })
            else:
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
        self._accumulate_usage(body.get("usage") or {})
        choice = body["choices"][0]
        text = choice["message"]["content"]
        if text is None:
            reason = choice.get("finish_reason", "unknown")
            raise ValueError(f"OpenRouter가 빈 응답을 반환했습니다 (finish_reason={reason}): {json.dumps(body)[:500]}")

        if json_mode:
            return self._parse_json(text)
        return text

    def _accumulate_usage(self, usage: dict):
        """usage 응답에서 토큰 통계를 누적한다.

        실측 확인 결과, OpenRouter는 캐시 통계를 최상위 필드(cache_creation_input_tokens/
        cache_read_input_tokens)가 아니라 usage.prompt_tokens_details.cache_write_tokens/
        cached_tokens에 담아 반환한다(Amazon Bedrock·Anthropic 직접 provider 둘 다 동일한
        형태로 실측 확인함, 2026-07-21). 과거 문서/코드는 Anthropic 원본 필드를 그대로
        패스스루한다고 가정했으나 실제로는 아니었다 — 그 결과 캐시 통계가 항상 0으로 잡혀
        cache_stats()가 실제 캐시 사용량을 반영하지 못하는 버그가 있었다.
        최상위 필드도 함께 확인해, 향후 응답 형태가 바뀌거나 다른 provider가 원본 필드를
        그대로 내려줘도 놓치지 않도록 한다(어느 쪽이든 값이 있으면 사용).
        """
        details = usage.get("prompt_tokens_details") or {}
        self._usage_totals["cache_read_input_tokens"] += int(
            usage.get("cache_read_input_tokens") or details.get("cached_tokens") or 0
        )
        self._usage_totals["cache_creation_input_tokens"] += int(
            usage.get("cache_creation_input_tokens") or details.get("cache_write_tokens") or 0
        )
        self._usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self._usage_totals["completion_tokens"] += int(usage.get("completion_tokens") or 0)

    def _post_chat(self, payload: dict):
        return requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": _APP_REFERER,
                "X-Title": _APP_TITLE,
            },
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

    def cache_stats(self) -> dict:
        """이 인스턴스가 수행한 모든 generate() 호출의 누적 프롬프트 캐시 통계.

        실제 값 추출 위치는 _accumulate_usage() 참고(usage.prompt_tokens_details 하위).
        캐시가 한 번도 없었으면 전부 0으로 채워 호출부가 매번 존재 여부를 따로
        확인하지 않아도 되게 한다.
        """
        t = self._usage_totals
        return {
            "cache_read_tokens": t["cache_read_input_tokens"],
            "cache_write_tokens": t["cache_creation_input_tokens"],
            "prompt_tokens": t["prompt_tokens"],
            "completion_tokens": t["completion_tokens"],
        }

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
