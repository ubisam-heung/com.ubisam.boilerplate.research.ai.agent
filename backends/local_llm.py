"""Ollama 기반 로컬 LLM 래퍼"""
import json
import requests


class LocalLLM:
    def __init__(self, model: str, base_url: str = "http://192.168.0.229:11345", temperature: float = 0.2):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature

    def generate(self, prompt: str, json_mode: bool = False, system: str = None) -> str | dict:
        """프롬프트를 보내고 응답을 받는다.

        json_mode=True 이면 응답을 JSON으로 파싱해서 dict/list로 반환한다.
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        resp = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=300)
        resp.raise_for_status()
        text = resp.json().get("response", "")

        if json_mode:
            return self._parse_json(text)
        return text

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
