"""
examples/embedding_basic.py — نمونه استفاده از EmbeddingClient

اجرا:
    uv run python examples/embedding_basic.py
"""

from src.embedding_client import EmbeddingClient

USER_ID = "user-123"


def example_single_embed() -> None:
    """تبدیل یک متن به بردار."""
    print("── Single Embed ─────────────────────────")
    emb = EmbeddingClient(user_id=USER_ID)

    vector = emb.embed("هوش مصنوعی آینده فناوری است.")
    print(f"ابعاد بردار: {len(vector)}")
    print(f"چند مقدار اول: {[round(v, 4) for v in vector[:5]]}\n")


def example_batch_embed() -> None:
    """تبدیل چند متن به بردار به صورت یکجا."""
    print("── Batch Embed ──────────────────────────")
    emb = EmbeddingClient(user_id=USER_ID)

    texts = [
        "هوش مصنوعی آینده فناوری است.",
        "یادگیری ماشین زیرمجموعه‌ای از هوش مصنوعی است.",
        "امروز هوا آفتابی است.",
    ]
    vectors = emb.embed_batch(texts)
    print(f"تعداد بردار: {len(vectors)} | ابعاد هر بردار: {len(vectors[0])}\n")


def example_similarity() -> None:
    """محاسبه شباهت cosine بین جمله‌ها."""
    print("── Cosine Similarity ────────────────────")
    emb = EmbeddingClient(user_id=USER_ID)

    sentences = [
        "گربه روی صندلی نشسته است.",
        "یک گربه روی مبل نشسته.",
        "هواپیما در آسمان پرواز می‌کند.",
    ]
    vectors = emb.embed_batch(sentences)

    for i in range(len(sentences)):
        for j in range(i + 1, len(sentences)):
            sim = EmbeddingClient.cosine_similarity(vectors[i], vectors[j])
            print(f"  {sim:.4f}  |  «{sentences[i][:25]}» ↔ «{sentences[j][:25]}»")
    print()


if __name__ == "__main__":
    example_single_embed()
    example_batch_embed()
    example_similarity()