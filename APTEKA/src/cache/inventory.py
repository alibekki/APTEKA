import logging
from datetime import datetime
from src.api.rubus_client import RubusClient
from src.db.database import ProductDB

logger = logging.getLogger(__name__)


class InventoryCache:
    """Manages product inventory: loads from RUBUS API, stores in SQLite."""

    def __init__(self, client: RubusClient):
        self._client = client
        self._product_db = ProductDB()
        self._last_load_time: str | None = None
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def product_count(self) -> int:
        return self._product_db.get_total_count()

    async def load_full(self):
        """Load full inventory from getRest into SQLite."""
        logger.info("Loading full inventory from RUBUS API...")
        data = await self._client._post("getRest")
        items = data.get("data", {}).get("items", [])

        self._product_db.upsert_products(items)
        self._product_db.rebuild_fts()
        self._last_load_time = datetime.now().strftime("%H:%M")
        self._loaded = True
        logger.info("Inventory loaded: %d products into DB, FTS5 rebuilt", len(items))

    async def refresh(self):
        """Incremental update via getRestOnTime."""
        if not self._loaded or not self._last_load_time:
            await self.load_full()
            return

        try:
            data = await self._client.get_rest_on_time(self._last_load_time)
            items = data.get("items", [])
            updated = 0

            for item in items:
                status = item.get("status")
                guid = item.get("guid", "")

                if status == "new":
                    self._product_db.upsert_products([item])
                    updated += 1
                elif status == "changed":
                    changed = item.get("changed", [])
                    fields = {}
                    if "rest" in changed:
                        fields["rest_abs"] = int(item.get("rest_abs", 0))
                        fields["rest_pack"] = int(item.get("rest_pack", 0))
                        fields["rest_piece"] = int(item.get("rest_piece", 0))
                    if "price" in changed:
                        fields["price"] = float(item.get("price", 0))
                    if "rest_rezerv" in changed:
                        fields["rest_rezerv"] = int(item.get("rest_rezerv", 0))
                    if fields:
                        self._product_db.update_stock(guid, **fields)
                        updated += 1

            self._last_load_time = datetime.now().strftime("%H:%M")
            if updated:
                logger.info("Cache refreshed: %d items updated", updated)
        except Exception as e:
            logger.error("Cache refresh failed: %s", e)
