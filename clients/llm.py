"""共用 Gemini Chat Model 工廠 (Supervisor 與各子圖共用同一份設定)"""
from functools import lru_cache

from langchain_google_genai import ChatGoogleGenerativeAI

import config


@lru_cache(maxsize=4)
def get_llm(model: str | None = None, temperature: float | None = None) -> ChatGoogleGenerativeAI:
    kwargs = {
        "model": model or config.GEMINI_MODEL,
        "google_api_key": config.GOOGLE_API_KEY,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatGoogleGenerativeAI(**kwargs)
