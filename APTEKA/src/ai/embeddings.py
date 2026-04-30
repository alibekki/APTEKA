import json
import logging
import struct
import numpy as np
from openai import AsyncOpenAI
from src.db.database import ProductDB

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536


def _serialize_embedding(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _deserialize_embedding(data: bytes) -> np.ndarray:
    count = len(data) // 4
    return np.array(struct.unpack(f"{count}f", data), dtype=np.float32)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    dot = np.dot(a, b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    if norm == 0:
        return 0.0
    return float(dot / norm)


class EmbeddingService:
    """Manages product embeddings for semantic search."""

    def __init__(self, api_key: str):
        self._client = AsyncOpenAI(api_key=api_key)
        self._product_db = ProductDB()
        self._embeddings_cache: dict[str, np.ndarray] = {}

    async def get_embedding(self, text: str) -> list[float]:
        """Get embedding vector for a text string."""
        response = await self._client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text,
        )
        return response.data[0].embedding

    async def get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """Get embeddings for multiple texts in one API call."""
        if not texts:
            return []
        response = await self._client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=texts,
        )
        return [item.embedding for item in response.data]

    async def build_product_embeddings(self, batch_size: int = 500):
        """Build embeddings for all products that don't have them yet."""
        products = self._product_db.get_products_without_ai_fields(limit=5000)
        if not products:
            logger.info("All products already have embeddings")
            return

        logger.info("Building embeddings for %d products...", len(products))
        total = 0

        for i in range(0, len(products), batch_size):
            batch = products[i:i + batch_size]
            texts = [
                f"{p['name']} {p.get('producer', '')} {p.get('groups', '')}"
                for p in batch
            ]

            try:
                embeddings = await self.get_embeddings_batch(texts)
                for product, embedding in zip(batch, embeddings):
                    serialized = _serialize_embedding(embedding)
                    self._product_db.update_embedding(product["guid"], serialized)
                total += len(batch)
                logger.info("Embedded %d/%d products", total, len(products))
            except Exception as e:
                logger.error("Embedding batch failed: %s", e)

    def load_cache(self):
        """Load all embeddings into memory for fast search."""
        rows = self._product_db.get_all_with_embeddings()
        self._embeddings_cache.clear()
        for row in rows:
            if row["embedding"]:
                self._embeddings_cache[row["guid"]] = _deserialize_embedding(row["embedding"])
        logger.info("Loaded %d embeddings into cache", len(self._embeddings_cache))

    async def semantic_search(self, query: str, limit: int = 10) -> list[tuple[str, float]]:
        """Search products by semantic similarity. Returns (guid, score) pairs."""
        if not self._embeddings_cache:
            self.load_cache()

        if not self._embeddings_cache:
            return []

        query_embedding = np.array(await self.get_embedding(query), dtype=np.float32)

        scored = []
        for guid, emb in self._embeddings_cache.items():
            score = _cosine_similarity(query_embedding, emb)
            scored.append((guid, score))

        scored.sort(key=lambda x: -x[1])
        return scored[:limit]
