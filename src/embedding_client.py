"""
embedding_client.py — کلاینت برای تبدیل متن به بردار (Embedding)
"""

import uuid
import httpx
from openai import OpenAI
from src.config import EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_API_KEY, CLIENT_ID
from src.debug.config import DebugConfig


def _make_http_client(user_id: str) -> httpx.Client:
    """یک httpx client با هدرهای ثابت مورد نیاز سرویس می‌سازد."""
    return httpx.Client(
        headers={
            "x-client-id": CLIENT_ID,
            "x-user-id": user_id,
            "x-request-id": str(uuid.uuid4()),
        }
    )


class EmbeddingClient:
    def __init__(self, user_id: str) -> None:
        self._client = OpenAI(
            base_url=EMBEDDING_BASE_URL,
            api_key=EMBEDDING_API_KEY,
            http_client=_make_http_client(user_id),
        )
        self._log = (DebugConfig.from_env()).get_logger("embedding_client")
        self.model = EMBEDDING_MODEL

    def embed(self, text: str) -> list[float]:
        """یک متن را به بردار عددی تبدیل می‌کند."""

        self._log.info(f"start embdedding {text}")
        response = self._client.embeddings.create(
            model=self.model,
            input=text,
        )
        self._log.info("finish embdedding")
        return response.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """چند متن را به صورت یکجا به بردار تبدیل می‌کند."""
  
        self._log.info("start batch embdedding", chunk_count=len(texts))
        response = self._client.embeddings.create(
            model=self.model,
            input=texts,
        )
        self._log.info("finish batch embdedding")
        # مرتب‌سازی بر اساس index تا ترتیب حفظ بشه
        sorted_data = sorted(response.data, key=lambda d: d.index)
        return [d.embedding for d in sorted_data]

    @staticmethod
    def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        """شباهت cosine بین دو بردار را محاسبه می‌کند (بدون نیاز به numpy)."""
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = sum(a ** 2 for a in vec_a) ** 0.5
        norm_b = sum(b ** 2 for b in vec_b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)