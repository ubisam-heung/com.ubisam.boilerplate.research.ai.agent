"""Ollama 기반 로컬 LLM 래퍼"""
import json
import re
import requests


class LocalLLM:
    def __init__(self, model: str, base_url: str = "http://192.168.0.229:11345", temperature: float = 0.2):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature

    def generate(self, prompt: str, json_mode: bool = False, system: str = None, num_predict: int = 512) -> str | dict:
        """프롬프트를 보내고 응답을 받는다.

        json_mode=True 이면 응답을 JSON으로 파싱해서 dict/list로 반환한다.
        num_predict: 최대 생성 토큰 수 (기본 512).
        """
        payload = {
            "model": self.model,
            "prompt": prompt + " /no_think",
            "stream": False,
            "think": False,
            "options": {"temperature": self.temperature, "num_predict": num_predict},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        resp = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=300)
        resp.raise_for_status()
        text = resp.json().get("response", "")
        text = self._strip_thinking(text)

        if json_mode:
            return self._parse_json(text)
        return text

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """Qwen3 계열 모델의 <think>...</think> 블록을 제거한다.

        블록 제거 후 텍스트가 비면 <think> 내부 내용을 폴백으로 반환한다.
        (모델이 응답 전체를 think 블록에 넣는 경우 대응)
        """
        stripped = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if stripped:
            return stripped
        # 폴백: think 블록 내용만 있는 경우 태그만 벗겨서 반환
        inner = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL)
        if inner:
            return inner.group(1).strip()
        return text.strip()

    @staticmethod
    def _parse_json(text: str):
        text = text.strip()
        # 코드펜스 제거
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM이 유효한 JSON을 반환하지 않았습니다: {text[:300]}") from e

    def health_check(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False
