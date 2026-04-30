import logging
import aiohttp
from src.config import RubusConfig
from src.models.product import Product

logger = logging.getLogger(__name__)


class RubusClient:
    def __init__(self, config: RubusConfig):
        self._config = config
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, method: str, extra: dict | None = None) -> dict:
        session = await self._get_session()
        url = f"{self._config.base_url}/{method}"
        payload = {**self._config.auth_payload}
        if extra:
            payload.update(extra)

        async with session.post(url, json=payload) as resp:
            data = await resp.json(content_type=None)

        if data.get("status") != "ok":
            msg = data.get("msg", "Unknown error")
            logger.error("RUBUS API error [%s]: %s", method, msg)
            raise RubusAPIError(method, msg)

        return data

    async def check_token(self) -> bool:
        try:
            await self._post("checkToken")
            return True
        except RubusAPIError:
            return False

    async def get_rest(self) -> list[Product]:
        data = await self._post("getRest")
        items = data.get("data", {}).get("items", [])
        products = []
        for item in items:
            try:
                products.append(Product.from_api(item))
            except (ValueError, KeyError) as e:
                logger.warning("Skipping malformed product: %s", e)
        logger.info("Loaded %d products from getRest", len(products))
        return products

    async def get_rest_on_time(self, time_start: str) -> dict:
        data = await self._post("getRestOnTime", {"time_start": time_start})
        result = data.get("data", {})
        return result


class RubusAPIError(Exception):
    def __init__(self, method: str, message: str):
        self.method = method
        self.message = message
        super().__init__(f"RUBUS [{method}]: {message}")
