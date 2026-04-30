import json
import os
import sqlite3
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from src.utils.transliterate import generate_search_variants
from src.utils.fuzzy import fuzzy_search, similarity_score

logger = logging.getLogger(__name__)

# DB location is configurable so the same image runs locally (./data) and on
# Fly.io (mounted volume at /data). Override with DATA_DIR=/path env var.
_DEFAULT_DATA_DIR = Path(__file__).parent.parent.parent / "data"
DATA_DIR = Path(os.getenv("DATA_DIR", str(_DEFAULT_DATA_DIR)))
DB_PATH = DATA_DIR / "apteka.db"

# Max products shown in a single WhatsApp list (WhatsApp text menu is comfortable up to 10)
MAX_PRODUCTS_IN_LIST = 8
# Max reply buttons WhatsApp/Wazzup allows in a single interactive message
MAX_INTERACTIVE_BUTTONS = 3


def _escape_like(value: str) -> str:
    """Escape special LIKE characters (%, _, \\) to prevent wildcard injection."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables including FTS5."""
    conn = get_connection()
    conn.executescript("""
        -- Products table (cached from RUBUS API)
        CREATE TABLE IF NOT EXISTS products (
            guid TEXT PRIMARY KEY,
            barcode TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            producer TEXT DEFAULT '',
            expiration_date TEXT DEFAULT '',
            pack INTEGER DEFAULT 1,
            rest_abs INTEGER DEFAULT 0,
            rest_rezerv INTEGER DEFAULT 0,
            rest_pack INTEGER DEFAULT 0,
            rest_piece INTEGER DEFAULT 0,
            price REAL DEFAULT 0,
            price_buy REAL DEFAULT 0,
            price_limit REAL DEFAULT 0,
            nds INTEGER DEFAULT 0,
            nds_vat INTEGER DEFAULT 0,
            reg_num TEXT DEFAULT '',
            margin TEXT DEFAULT '',
            series TEXT DEFAULT '',
            tnvd TEXT DEFAULT '',
            note TEXT DEFAULT '',
            groups TEXT DEFAULT '',
            no_discount INTEGER DEFAULT 0,
            is_prescription INTEGER DEFAULT 0,
            active_ingredient TEXT DEFAULT '',
            therapeutic_category TEXT DEFAULT '',
            embedding BLOB,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_products_name_lower ON products(name_lower);
        CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);
        CREATE INDEX IF NOT EXISTS idx_products_active_ingredient ON products(active_ingredient);
        CREATE INDEX IF NOT EXISTS idx_products_therapeutic_category ON products(therapeutic_category);
        CREATE INDEX IF NOT EXISTS idx_products_is_prescription ON products(is_prescription);

        -- FTS5 full-text search index
        CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
            name,
            producer,
            active_ingredient,
            groups,
            content=products,
            content_rowid=rowid,
            tokenize='unicode61 remove_diacritics 2'
        );

        -- Triggers to keep FTS5 in sync with products table
        CREATE TRIGGER IF NOT EXISTS products_ai AFTER INSERT ON products BEGIN
            INSERT INTO products_fts(rowid, name, producer, active_ingredient, groups)
            VALUES (new.rowid, new.name, new.producer, new.active_ingredient, new.groups);
        END;

        CREATE TRIGGER IF NOT EXISTS products_ad AFTER DELETE ON products BEGIN
            INSERT INTO products_fts(products_fts, rowid, name, producer, active_ingredient, groups)
            VALUES ('delete', old.rowid, old.name, old.producer, old.active_ingredient, old.groups);
        END;

        CREATE TRIGGER IF NOT EXISTS products_au AFTER UPDATE ON products BEGIN
            INSERT INTO products_fts(products_fts, rowid, name, producer, active_ingredient, groups)
            VALUES ('delete', old.rowid, old.name, old.producer, old.active_ingredient, old.groups);
            INSERT INTO products_fts(rowid, name, producer, active_ingredient, groups)
            VALUES (new.rowid, new.name, new.producer, new.active_ingredient, new.groups);
        END;

        -- Users table (supports Telegram + WhatsApp)
        -- chat_id: Telegram numeric ID or WhatsApp phone number
        -- platform: 'telegram' or 'whatsapp'
        CREATE TABLE IF NOT EXISTS users (
            chat_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL DEFAULT 'telegram',
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Search history
        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            query TEXT NOT NULL,
            results_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES users(chat_id)
        );

        CREATE INDEX IF NOT EXISTS idx_search_history_chat ON search_history(chat_id);
        CREATE INDEX IF NOT EXISTS idx_search_history_date ON search_history(created_at);

        -- Favorites
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            product_guid TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES users(chat_id),
            FOREIGN KEY (product_guid) REFERENCES products(guid),
            UNIQUE(chat_id, product_guid)
        );

        -- Stock notifications (notify when product becomes available)
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            product_guid TEXT NOT NULL,
            product_name TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notified_at TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES users(chat_id),
            UNIQUE(chat_id, product_guid)
        );

        -- Conversation messages for context
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES users(chat_id)
        );

        CREATE INDEX IF NOT EXISTS idx_conversations_chat ON conversations(chat_id);

        -- WhatsApp session state (persistent replacement for in-memory dict).
        -- Keeps last shown product list + currently viewed product so that
        -- intents like "добавь первый в избранное" resolve across restarts.
        CREATE TABLE IF NOT EXISTS wa_sessions (
            chat_id TEXT PRIMARY KEY,
            last_products_json TEXT DEFAULT '[]',
            current_product_guid TEXT DEFAULT '',
            last_intent TEXT DEFAULT '',
            page_offset INTEGER DEFAULT 0,
            last_query TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Migration: add platform column if missing (for existing DBs)
    _migrate_add_platform(conn)
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


def _migrate_add_platform(conn: sqlite3.Connection):
    """Add platform column to users table if it doesn't exist (migration)."""
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "platform" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'")
            logger.info("Migration: added 'platform' column to users table")
    except Exception as e:
        logger.debug("Platform migration check: %s", e)


class ProductDB:
    """Product database operations."""

    _fuzzy_cache: list[dict] | None = None
    _fuzzy_cache_time: float = 0
    _fuzzy_cache_lock = threading.Lock()

    def __init__(self):
        self._conn = get_connection()

    def upsert_products(self, products: list[dict]):
        """Bulk upsert products from API response."""
        cursor = self._conn.cursor()
        for p in products:
            name = p.get("name", "").strip()
            groups = p.get("groups", "")
            is_rx = 1 if "рецептурн" in groups.lower() else 0

            cursor.execute("""
                INSERT INTO products (guid, barcode, name, name_lower, producer,
                    expiration_date, pack, rest_abs, rest_rezerv, rest_pack, rest_piece,
                    price, price_buy, price_limit, nds, nds_vat, reg_num, margin,
                    series, tnvd, note, groups, no_discount, is_prescription, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guid) DO UPDATE SET
                    barcode=excluded.barcode, name=excluded.name, name_lower=excluded.name_lower,
                    producer=excluded.producer, expiration_date=excluded.expiration_date,
                    pack=excluded.pack, rest_abs=excluded.rest_abs, rest_rezerv=excluded.rest_rezerv,
                    rest_pack=excluded.rest_pack, rest_piece=excluded.rest_piece,
                    price=excluded.price, price_buy=excluded.price_buy, price_limit=excluded.price_limit,
                    nds=excluded.nds, nds_vat=excluded.nds_vat, reg_num=excluded.reg_num,
                    margin=excluded.margin, series=excluded.series, tnvd=excluded.tnvd,
                    note=excluded.note, groups=excluded.groups, no_discount=excluded.no_discount,
                    is_prescription=excluded.is_prescription, updated_at=excluded.updated_at
            """, (
                p.get("guid", ""),
                p.get("barcode", ""),
                name,
                name.lower(),
                p.get("producer", "").strip(),
                p.get("expiration_date", ""),
                int(p.get("pack", 1)),
                int(p.get("rest_abs", 0)),
                int(p.get("rest_rezerv", 0)),
                int(p.get("rest_pack", 0)),
                int(p.get("rest_piece", 0)),
                float(p.get("price", 0)),
                float(p.get("price_buy", 0)),
                float(p.get("price_limit", 0) or 0),
                int(p.get("nds", 0)),
                int(p.get("nds_vat", 0)),
                p.get("reg_num", ""),
                p.get("margin", ""),
                p.get("series", ""),
                p.get("tnvd", ""),
                p.get("note", ""),
                p.get("groups", ""),
                int(p.get("no_discount", 0)),
                is_rx,
                datetime.now().isoformat(),
            ))
        self._conn.commit()

    def update_stock(self, guid: str, **fields):
        """Update specific fields for a product."""
        allowed = {"rest_abs", "rest_pack", "rest_piece", "rest_rezerv", "price"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [datetime.now().isoformat(), guid]
        self._conn.execute(
            f"UPDATE products SET {set_clause}, updated_at=? WHERE guid=?",
            values
        )
        self._conn.commit()

    def rebuild_fts(self):
        """Rebuild FTS5 index from scratch."""
        self._conn.execute("INSERT INTO products_fts(products_fts) VALUES('rebuild')")
        self._conn.commit()
        logger.info("FTS5 index rebuilt")

    def search_fts(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search using FTS5 with ranking.

        Tries multiple FTS strategies:
        1. All terms with prefix match (AND): "парац"* "табл"*
        2. First term only with prefix (for multi-word queries)
        """
        safe_query = query.replace('"', '""')
        terms = [t for t in safe_query.split() if t]
        if not terms:
            return []

        results: dict[str, dict] = {}

        # Strategy A: All terms with prefix (most precise)
        fts_query = " ".join(f'"{t}"*' for t in terms)
        try:
            rows = self._conn.execute("""
                SELECT p.*, rank
                FROM products_fts fts
                JOIN products p ON p.rowid = fts.rowid
                WHERE products_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit)).fetchall()
            for r in rows:
                d = dict(r)
                results[d["guid"]] = d
        except Exception as e:
            logger.debug("FTS5 AND-query failed: %s", e)

        # Strategy B: First (main) term only — catches more results
        if len(terms) > 1 and len(results) < limit:
            main_term = max(terms, key=len)  # longest term is likely the drug name
            fts_query_main = f'"{main_term}"*'
            try:
                rows = self._conn.execute("""
                    SELECT p.*, rank
                    FROM products_fts fts
                    JOIN products p ON p.rowid = fts.rowid
                    WHERE products_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query_main, limit)).fetchall()
                for r in rows:
                    d = dict(r)
                    if d["guid"] not in results:
                        results[d["guid"]] = d
            except Exception as e:
                logger.debug("FTS5 single-term query failed: %s", e)

        return list(results.values())[:limit]

    def search_by_name(self, query: str, limit: int = 10) -> list[dict]:
        """Search products by name substring (LIKE-based fallback)."""
        escaped = _escape_like(query.lower().strip())
        rows = self._conn.execute("""
            SELECT * FROM products
            WHERE name_lower LIKE ? ESCAPE '\\'
            ORDER BY
                CASE WHEN name_lower LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,
                (rest_abs - rest_rezerv) DESC,
                price ASC
            LIMIT ?
        """, (f"%{escaped}%", f"{escaped}%", limit)).fetchall()
        return [dict(r) for r in rows]

    def search_by_barcode(self, barcode: str) -> list[dict]:
        """Search product by barcode."""
        rows = self._conn.execute(
            "SELECT * FROM products WHERE barcode = ?", (barcode,)
        ).fetchall()
        return [dict(r) for r in rows]

    def search_by_active_ingredient(self, ingredient: str, limit: int = 10) -> list[dict]:
        """Search products by active ingredient."""
        escaped = _escape_like(ingredient.lower())
        rows = self._conn.execute("""
            SELECT * FROM products
            WHERE active_ingredient LIKE ? ESCAPE '\\'
            ORDER BY (rest_abs - rest_rezerv) DESC, price ASC
            LIMIT ?
        """, (f"%{escaped}%", limit)).fetchall()
        return [dict(r) for r in rows]

    def get_all_products_light(self) -> list[dict]:
        """Get all products with minimal fields for fuzzy search.
        Cached in memory for 120 seconds to avoid repeated DB hits.
        Thread-safe via lock.
        """
        now = time.time()
        with ProductDB._fuzzy_cache_lock:
            if ProductDB._fuzzy_cache is not None and (now - ProductDB._fuzzy_cache_time) < 120:
                return ProductDB._fuzzy_cache

            rows = self._conn.execute("""
                SELECT guid, name, name_lower, producer, barcode, groups,
                       active_ingredient, rest_abs, rest_rezerv, price,
                       is_prescription
                FROM products
            """).fetchall()
            ProductDB._fuzzy_cache = [dict(r) for r in rows]
            ProductDB._fuzzy_cache_time = now
            return ProductDB._fuzzy_cache

    def smart_search(self, query: str, limit: int = 10) -> list[dict]:
        """Multi-strategy smart search pipeline.

        Strategy priority:
        1. Barcode exact match (if query looks like a barcode)
        2. FTS5 full-text search (fast, handles word forms)
        3. FTS5 with transliterated variants (latin→cyrillic, cyrillic→latin)
        4. LIKE substring search with transliterated variants
        5. Fuzzy matching across all products (catches typos)

        Results are deduplicated and scored.
        """
        query = query.strip()
        if not query:
            return []

        results: dict[str, dict] = {}  # guid → product (dedup)
        scores: dict[str, float] = {}  # guid → best score

        def _add_results(products: list[dict], base_score: float):
            for i, p in enumerate(products):
                guid = p.get("guid", "")
                if not guid:
                    continue
                # Higher base_score = higher priority strategy
                score = base_score - (i * 0.01)  # preserve order within strategy
                if guid not in scores or score > scores[guid]:
                    scores[guid] = score
                    results[guid] = p

        # Strategy 1: Barcode (exact match, highest priority)
        if query.isdigit() and len(query) >= 4:
            barcode_results = self.search_by_barcode(query)
            _add_results(barcode_results, 100.0)
            if barcode_results:
                return list(results.values())[:limit]

        # Generate search variants (original + transliterated)
        variants = generate_search_variants(query)

        # Strategy 2: FTS5 search for each variant
        for variant in variants:
            fts_results = self.search_fts(variant, limit=15)
            _add_results(fts_results, 80.0)

        # Strategy 3: LIKE substring search for each variant
        for variant in variants:
            like_results = self.search_by_name(variant, limit=10)
            _add_results(like_results, 60.0)

        # Strategy 4: Active ingredient search
        for variant in variants:
            ingredient_results = self.search_by_active_ingredient(variant, limit=5)
            _add_results(ingredient_results, 50.0)

        # Strategy 5: Fuzzy matching (only if no results from fast strategies)
        if len(results) == 0:
            all_products = self.get_all_products_light()
            for variant in variants:
                fuzzy_results = fuzzy_search(variant, all_products, limit=10, threshold=0.45)
                _add_results(fuzzy_results, 40.0)

            # For fuzzy results, re-score with actual similarity
            for guid in list(results.keys()):
                if scores[guid] < 50:  # only re-score fuzzy results
                    product = results[guid]
                    best_sim = 0
                    for variant in variants:
                        sim = similarity_score(variant, product.get("name", ""))
                        best_sim = max(best_sim, sim)
                    scores[guid] = 40.0 + best_sim

        # If fuzzy results are light (missing fields), fetch full records
        guids_needing_full = [
            guid for guid, p in results.items()
            if "price_buy" not in p  # light record indicator
        ]
        if guids_needing_full:
            for guid in guids_needing_full:
                full = self.get_by_guid(guid)
                if full:
                    results[guid] = full

        # Sort by score descending, then in-stock first, then price ascending
        sorted_results = sorted(
            results.values(),
            key=lambda p: (
                -scores.get(p.get("guid", ""), 0),
                0 if (p.get("rest_abs", 0) - p.get("rest_rezerv", 0)) > 0 else 1,
                p.get("price", 0),
            ),
        )

        return sorted_results[:limit]

    def get_by_guid(self, guid: str) -> dict | None:
        """Get single product by GUID."""
        row = self._conn.execute(
            "SELECT * FROM products WHERE guid = ?", (guid,)
        ).fetchone()
        return dict(row) if row else None

    def get_in_stock(self, limit: int = 50) -> list[dict]:
        """Get products currently in stock."""
        rows = self._conn.execute("""
            SELECT * FROM products
            WHERE (rest_abs - rest_rezerv) > 0
            ORDER BY name_lower
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def find_analogs_by_ingredient(self, ingredient: str, exclude_guid: str = "",
                                    limit: int = 5) -> list[dict]:
        """Find analog products with same active ingredient."""
        escaped = _escape_like(ingredient.lower())
        rows = self._conn.execute("""
            SELECT * FROM products
            WHERE active_ingredient LIKE ? ESCAPE '\\'
            AND guid != ?
            AND (rest_abs - rest_rezerv) > 0
            ORDER BY price ASC
            LIMIT ?
        """, (f"%{escaped}%", exclude_guid, limit)).fetchall()
        return [dict(r) for r in rows]

    def find_analogs_by_category(self, category: str, exclude_guid: str = "",
                                  limit: int = 5) -> list[dict]:
        """Find analog products in same therapeutic category."""
        escaped = _escape_like(category.lower())
        rows = self._conn.execute("""
            SELECT * FROM products
            WHERE therapeutic_category LIKE ? ESCAPE '\\'
            AND guid != ?
            AND (rest_abs - rest_rezerv) > 0
            ORDER BY price ASC
            LIMIT ?
        """, (f"%{escaped}%", exclude_guid, limit)).fetchall()
        return [dict(r) for r in rows]

    def update_product_ai_fields(self, guid: str, active_ingredient: str = "",
                                  therapeutic_category: str = ""):
        """Update AI-enriched fields."""
        self._conn.execute("""
            UPDATE products SET active_ingredient=?, therapeutic_category=?
            WHERE guid=?
        """, (active_ingredient.lower(), therapeutic_category.lower(), guid))
        self._conn.commit()

    def update_embedding(self, guid: str, embedding: bytes):
        """Store embedding vector for a product."""
        self._conn.execute(
            "UPDATE products SET embedding=? WHERE guid=?", (embedding, guid)
        )
        self._conn.commit()

    def get_all_with_embeddings(self) -> list[dict]:
        """Get all products that have embeddings."""
        rows = self._conn.execute(
            "SELECT guid, name, embedding FROM products WHERE embedding IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_products_without_ai_fields(self, limit: int = 100) -> list[dict]:
        """Get products that haven't been enriched with AI fields yet."""
        rows = self._conn.execute("""
            SELECT guid, name, producer, groups FROM products
            WHERE active_ingredient = '' OR active_ingredient IS NULL
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_total_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM products").fetchone()
        return row["cnt"]

    def get_in_stock_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM products WHERE (rest_abs - rest_rezerv) > 0"
        ).fetchone()
        return row["cnt"]

    def get_prescription_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM products WHERE is_prescription = 1"
        ).fetchone()
        return row["cnt"]

    def get_total_value(self) -> float:
        row = self._conn.execute(
            "SELECT SUM(price * rest_abs) as total FROM products WHERE rest_abs > 0"
        ).fetchone()
        return row["total"] or 0

    def get_out_of_stock_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM products WHERE (rest_abs - rest_rezerv) <= 0"
        ).fetchone()
        return row["cnt"]

    def get_low_stock(self, threshold: int = 3, limit: int = 100) -> list[dict]:
        """Products with stock ≤ threshold (and > 0)."""
        rows = self._conn.execute("""
            SELECT * FROM products
            WHERE (rest_abs - rest_rezerv) > 0 AND (rest_abs - rest_rezerv) <= ?
            ORDER BY (rest_abs - rest_rezerv) ASC, name_lower
            LIMIT ?
        """, (threshold, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_expiring_soon(self, days: int = 90, limit: int = 100) -> list[dict]:
        """Products whose expiration_date falls within the next `days`.

        expiration_date is stored as DD.MM.YYYY string — parse in Python.
        """
        from datetime import date, timedelta
        cutoff = date.today() + timedelta(days=days)
        today = date.today()
        rows = self._conn.execute("""
            SELECT * FROM products
            WHERE expiration_date IS NOT NULL AND expiration_date != ''
              AND (rest_abs - rest_rezerv) > 0
        """).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            exp = (d.get("expiration_date") or "").strip()
            try:
                parts = exp.split(".")
                if len(parts) != 3:
                    continue
                day, mon, year = int(parts[0]), int(parts[1]), int(parts[2])
                exp_date = date(year, mon, day)
            except (ValueError, IndexError):
                continue
            if today <= exp_date <= cutoff:
                d["_days_left"] = (exp_date - today).days
                result.append(d)
        result.sort(key=lambda p: p["_days_left"])
        return result[:limit]

    def search_advanced(self, query: str = "", *, in_stock_only: bool = False,
                        rx_only: bool = False, otc_only: bool = False,
                        producer: str = "", min_price: float = 0, max_price: float = 0,
                        sort: str = "name", limit: int = 50, offset: int = 0) -> list[dict]:
        """Filter + sort products. `sort` ∈ {name, price_asc, price_desc, stock_desc, stock_asc}."""
        where: list[str] = ["1=1"]
        args: list = []
        if query:
            escaped = _escape_like(query.lower())
            where.append("name_lower LIKE ? ESCAPE '\\'")
            args.append(f"%{escaped}%")
        if in_stock_only:
            where.append("(rest_abs - rest_rezerv) > 0")
        if rx_only:
            where.append("is_prescription = 1")
        if otc_only:
            where.append("is_prescription = 0")
        if producer:
            where.append("LOWER(producer) LIKE ? ESCAPE '\\'")
            args.append(f"%{_escape_like(producer.lower())}%")
        if min_price > 0:
            where.append("price >= ?")
            args.append(min_price)
        if max_price > 0:
            where.append("price <= ?")
            args.append(max_price)

        order = {
            "name": "name_lower ASC",
            "price_asc": "price ASC",
            "price_desc": "price DESC",
            "stock_desc": "(rest_abs - rest_rezerv) DESC",
            "stock_asc": "(rest_abs - rest_rezerv) ASC",
        }.get(sort, "name_lower ASC")

        args.extend([limit, offset])
        sql = f"""
            SELECT * FROM products
            WHERE {' AND '.join(where)}
            ORDER BY {order}
            LIMIT ? OFFSET ?
        """
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def count_advanced(self, query: str = "", *, in_stock_only: bool = False,
                       rx_only: bool = False, otc_only: bool = False,
                       producer: str = "", min_price: float = 0, max_price: float = 0) -> int:
        where: list[str] = ["1=1"]
        args: list = []
        if query:
            escaped = _escape_like(query.lower())
            where.append("name_lower LIKE ? ESCAPE '\\'")
            args.append(f"%{escaped}%")
        if in_stock_only:
            where.append("(rest_abs - rest_rezerv) > 0")
        if rx_only:
            where.append("is_prescription = 1")
        if otc_only:
            where.append("is_prescription = 0")
        if producer:
            where.append("LOWER(producer) LIKE ? ESCAPE '\\'")
            args.append(f"%{_escape_like(producer.lower())}%")
        if min_price > 0:
            where.append("price >= ?")
            args.append(min_price)
        if max_price > 0:
            where.append("price <= ?")
            args.append(max_price)
        row = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM products WHERE {' AND '.join(where)}", args,
        ).fetchone()
        return row["cnt"]

    def get_top_producers(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute("""
            SELECT producer, COUNT(*) as cnt,
                   SUM(CASE WHEN (rest_abs - rest_rezerv) > 0 THEN 1 ELSE 0 END) as in_stock_cnt,
                   AVG(price) as avg_price
            FROM products
            WHERE producer != '' AND producer IS NOT NULL
            GROUP BY producer
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_top_categories(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute("""
            SELECT therapeutic_category, COUNT(*) as cnt
            FROM products
            WHERE therapeutic_category != '' AND therapeutic_category IS NOT NULL
            GROUP BY therapeutic_category
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_enrichment_progress(self) -> dict:
        row = self._conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN active_ingredient != '' AND active_ingredient IS NOT NULL
                         THEN 1 ELSE 0 END) as enriched,
                SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) as embedded
            FROM products
        """).fetchone()
        return dict(row) if row else {"total": 0, "enriched": 0, "embedded": 0}

    def latest_updated_at(self) -> str:
        row = self._conn.execute(
            "SELECT MAX(updated_at) as ts FROM products"
        ).fetchone()
        return row["ts"] or ""


class UserDB:
    """User database operations."""

    def __init__(self):
        self._conn = get_connection()

    def upsert_user(self, chat_id: int | str, username: str = "", first_name: str = "",
                     last_name: str = "", platform: str = "telegram"):
        self._conn.execute("""
            INSERT INTO users (chat_id, platform, username, first_name, last_name, last_active)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username=excluded.username, first_name=excluded.first_name,
                last_name=excluded.last_name, last_active=excluded.last_active
        """, (str(chat_id), platform, username, first_name, last_name,
              datetime.now().isoformat()))
        self._conn.commit()

    def log_search(self, chat_id: int | str, query: str, results_count: int):
        self._conn.execute("""
            INSERT INTO search_history (chat_id, query, results_count)
            VALUES (?, ?, ?)
        """, (str(chat_id), query, results_count))
        self._conn.commit()

    def get_search_history(self, chat_id: int | str, limit: int = 20) -> list[dict]:
        rows = self._conn.execute("""
            SELECT query, results_count, created_at FROM search_history
            WHERE chat_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (str(chat_id), limit)).fetchall()
        return [dict(r) for r in rows]

    def add_favorite(self, chat_id: int | str, product_guid: str):
        self._conn.execute("""
            INSERT OR IGNORE INTO favorites (chat_id, product_guid)
            VALUES (?, ?)
        """, (str(chat_id), product_guid))
        self._conn.commit()

    def remove_favorite(self, chat_id: int | str, product_guid: str):
        self._conn.execute(
            "DELETE FROM favorites WHERE chat_id=? AND product_guid=?",
            (str(chat_id), product_guid)
        )
        self._conn.commit()

    def get_favorites(self, chat_id: int | str) -> list[dict]:
        rows = self._conn.execute("""
            SELECT p.* FROM favorites f
            JOIN products p ON f.product_guid = p.guid
            WHERE f.chat_id = ?
            ORDER BY f.created_at DESC
        """, (str(chat_id),)).fetchall()
        return [dict(r) for r in rows]

    def is_favorite(self, chat_id: int | str, product_guid: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM favorites WHERE chat_id=? AND product_guid=?",
            (str(chat_id), product_guid)
        ).fetchone()
        return row is not None

    def add_notification(self, chat_id: int | str, product_guid: str, product_name: str):
        self._conn.execute("""
            INSERT OR REPLACE INTO notifications (chat_id, product_guid, product_name, is_active)
            VALUES (?, ?, ?, 1)
        """, (str(chat_id), product_guid, product_name))
        self._conn.commit()

    def get_active_notifications(self) -> list[dict]:
        """Get all active notifications with product stock info and user platform."""
        rows = self._conn.execute("""
            SELECT n.*, p.rest_abs, p.rest_rezerv, u.platform
            FROM notifications n
            JOIN products p ON n.product_guid = p.guid
            JOIN users u ON n.chat_id = u.chat_id
            WHERE n.is_active = 1
        """).fetchall()
        return [dict(r) for r in rows]

    def mark_notified(self, notification_id: int):
        self._conn.execute("""
            UPDATE notifications SET is_active=0, notified_at=?
            WHERE id=?
        """, (datetime.now().isoformat(), notification_id))
        self._conn.commit()

    def get_user_notifications(self, chat_id: int | str) -> list[dict]:
        rows = self._conn.execute("""
            SELECT * FROM notifications
            WHERE chat_id=? AND is_active=1
        """, (str(chat_id),)).fetchall()
        return [dict(r) for r in rows]

    def save_message(self, chat_id: int | str, role: str, content: str):
        self._conn.execute("""
            INSERT INTO conversations (chat_id, role, content)
            VALUES (?, ?, ?)
        """, (str(chat_id), role, content))
        self._conn.commit()

    def cleanup_old_conversations(self, max_age_days: int = 7, max_per_user: int = 50):
        """Remove old conversation messages to prevent unbounded growth."""
        self._conn.execute("""
            DELETE FROM conversations
            WHERE created_at < datetime('now', ?)
        """, (f"-{max_age_days} days",))
        self._conn.execute("""
            DELETE FROM conversations WHERE id IN (
                SELECT id FROM conversations c
                WHERE (SELECT COUNT(*) FROM conversations c2
                       WHERE c2.chat_id = c.chat_id AND c2.id >= c.id) > ?
            )
        """, (max_per_user,))
        self._conn.commit()

    def get_conversation(self, chat_id: int | str, limit: int = 10) -> list[dict]:
        rows = self._conn.execute("""
            SELECT role, content FROM conversations
            WHERE chat_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (str(chat_id), limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_conversation(self, chat_id: int | str):
        self._conn.execute(
            "DELETE FROM conversations WHERE chat_id=?", (str(chat_id),)
        )
        self._conn.commit()

    def get_total_users(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        return row["cnt"]

    def get_total_searches(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM search_history").fetchone()
        return row["cnt"]

    def get_popular_queries(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute("""
            SELECT query, COUNT(*) as count, AVG(results_count) as avg_results
            FROM search_history
            GROUP BY LOWER(query)
            ORDER BY count DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_recent_users(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute("""
            SELECT u.*, COUNT(s.id) as search_count
            FROM users u
            LEFT JOIN search_history s ON u.chat_id = s.chat_id
            GROUP BY u.chat_id
            ORDER BY u.last_active DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_searches_today(self) -> int:
        row = self._conn.execute("""
            SELECT COUNT(*) as cnt FROM search_history
            WHERE DATE(created_at) = DATE('now')
        """).fetchone()
        return row["cnt"]

    def get_searches_since(self, days: int) -> int:
        row = self._conn.execute("""
            SELECT COUNT(*) as cnt FROM search_history
            WHERE created_at >= datetime('now', ?)
        """, (f"-{days} days",)).fetchone()
        return row["cnt"]

    def get_searches_timeline(self, days: int = 14) -> list[dict]:
        """Returns one row per day for the last `days`, filling zeros."""
        rows = self._conn.execute("""
            SELECT DATE(created_at) as day, COUNT(*) as cnt
            FROM search_history
            WHERE created_at >= datetime('now', ?)
            GROUP BY day
            ORDER BY day
        """, (f"-{days} days",)).fetchall()
        by_day = {r["day"]: r["cnt"] for r in rows}
        from datetime import date, timedelta
        today = date.today()
        result = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            key = d.strftime("%Y-%m-%d")
            result.append({"day": key, "cnt": by_day.get(key, 0)})
        return result

    def get_hourly_activity(self) -> list[dict]:
        """Distribution of searches by hour of day over last 7d."""
        rows = self._conn.execute("""
            SELECT CAST(strftime('%H', created_at) AS INTEGER) as hour, COUNT(*) as cnt
            FROM search_history
            WHERE created_at >= datetime('now', '-7 days')
            GROUP BY hour
            ORDER BY hour
        """).fetchall()
        by_hour = {r["hour"]: r["cnt"] for r in rows}
        return [{"hour": h, "cnt": by_hour.get(h, 0)} for h in range(24)]

    def get_platform_split(self) -> dict:
        """Users and searches split by platform."""
        rows = self._conn.execute("""
            SELECT platform, COUNT(*) as users_cnt FROM users GROUP BY platform
        """).fetchall()
        users = {r["platform"]: r["users_cnt"] for r in rows}

        rows2 = self._conn.execute("""
            SELECT u.platform, COUNT(s.id) as searches_cnt
            FROM search_history s
            JOIN users u ON s.chat_id = u.chat_id
            GROUP BY u.platform
        """).fetchall()
        searches = {r["platform"]: r["searches_cnt"] for r in rows2}

        platforms = set(users) | set(searches)
        return {
            "platforms": sorted(platforms),
            "users": [users.get(p, 0) for p in sorted(platforms)],
            "searches": [searches.get(p, 0) for p in sorted(platforms)],
        }

    def get_zero_result_queries(self, limit: int = 30) -> list[dict]:
        """Queries that returned 0 products — gaps in inventory."""
        rows = self._conn.execute("""
            SELECT query, COUNT(*) as count, MAX(created_at) as last_seen
            FROM search_history
            WHERE results_count = 0
            GROUP BY LOWER(query)
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_recent_activity(self, limit: int = 30) -> list[dict]:
        """Live feed: most recent search_history entries with user info."""
        rows = self._conn.execute("""
            SELECT s.query, s.results_count, s.created_at,
                   u.chat_id, u.first_name, u.last_name, u.username, u.platform
            FROM search_history s
            LEFT JOIN users u ON s.chat_id = u.chat_id
            ORDER BY s.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_user(self, chat_id: int | str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE chat_id = ?", (str(chat_id),),
        ).fetchone()
        return dict(row) if row else None

    def get_user_stats(self, chat_id: int | str) -> dict:
        """Per-user aggregate metrics."""
        cid = str(chat_id)
        searches = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM search_history WHERE chat_id=?", (cid,),
        ).fetchone()["cnt"]
        favs = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM favorites WHERE chat_id=?", (cid,),
        ).fetchone()["cnt"]
        notifs = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM notifications WHERE chat_id=? AND is_active=1", (cid,),
        ).fetchone()["cnt"]
        msgs = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE chat_id=?", (cid,),
        ).fetchone()["cnt"]
        return {"searches": searches, "favorites": favs,
                "notifications": notifs, "messages": msgs}

    def list_users(self, *, platform: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        where = ["1=1"]
        args: list = []
        if platform:
            where.append("u.platform = ?")
            args.append(platform)
        args.extend([limit, offset])
        rows = self._conn.execute(f"""
            SELECT u.*, COUNT(s.id) as search_count
            FROM users u
            LEFT JOIN search_history s ON u.chat_id = s.chat_id
            WHERE {' AND '.join(where)}
            GROUP BY u.chat_id
            ORDER BY u.last_active DESC
            LIMIT ? OFFSET ?
        """, args).fetchall()
        return [dict(r) for r in rows]

    def count_users(self, *, platform: str = "") -> int:
        if platform:
            row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE platform=?", (platform,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        return row["cnt"]

    def list_conversations(self, limit: int = 100) -> list[dict]:
        """One row per chat: last message timestamp + message count."""
        rows = self._conn.execute("""
            SELECT c.chat_id, u.first_name, u.last_name, u.username, u.platform,
                   COUNT(c.id) as msg_count,
                   MAX(c.created_at) as last_message_at,
                   (SELECT content FROM conversations
                    WHERE chat_id = c.chat_id ORDER BY created_at DESC LIMIT 1) as last_message
            FROM conversations c
            LEFT JOIN users u ON c.chat_id = u.chat_id
            GROUP BY c.chat_id
            ORDER BY last_message_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def full_conversation(self, chat_id: int | str, limit: int = 500) -> list[dict]:
        rows = self._conn.execute("""
            SELECT role, content, created_at FROM conversations
            WHERE chat_id = ? ORDER BY created_at ASC LIMIT ?
        """, (str(chat_id), limit)).fetchall()
        return [dict(r) for r in rows]

    def get_user_favorites(self, chat_id: int | str) -> list[dict]:
        return self.get_favorites(chat_id)

    def top_favorited_products(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute("""
            SELECT p.guid, p.name, p.price, p.rest_abs, p.rest_rezerv,
                   p.is_prescription, COUNT(f.id) as fav_count
            FROM favorites f
            JOIN products p ON f.product_guid = p.guid
            GROUP BY p.guid
            ORDER BY fav_count DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def all_active_notifications_admin(self) -> list[dict]:
        rows = self._conn.execute("""
            SELECT n.*, u.first_name, u.last_name, u.username, u.platform,
                   p.rest_abs, p.rest_rezerv
            FROM notifications n
            LEFT JOIN users u ON n.chat_id = u.chat_id
            LEFT JOIN products p ON n.product_guid = p.guid
            WHERE n.is_active = 1
            ORDER BY n.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def most_wanted_out_of_stock(self, limit: int = 20) -> list[dict]:
        """Products people subscribe to while out of stock — signals buying demand."""
        rows = self._conn.execute("""
            SELECT n.product_guid, n.product_name, COUNT(*) as subscribers
            FROM notifications n
            WHERE n.is_active = 1
            GROUP BY n.product_guid
            ORDER BY subscribers DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_recent_messages_feed(self, limit: int = 40) -> list[dict]:
        """Recent inbound+AI messages across chats — admin can watch in real-time."""
        rows = self._conn.execute("""
            SELECT c.role, c.content, c.created_at,
                   u.first_name, u.last_name, u.platform, c.chat_id
            FROM conversations c
            LEFT JOIN users u ON c.chat_id = u.chat_id
            ORDER BY c.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


class WASessionDB:
    """Persistent WhatsApp session state.

    Replaces the in-memory `_user_products` dict so that across restarts
    and multiple processes the bot still knows which list a user saw,
    which product they're drilling into, and where pagination stopped.
    """

    def __init__(self):
        self._conn = get_connection()

    def save_list(self, chat_id: str, products: list[dict], query: str = "",
                  intent: str = "", page_offset: int = 0):
        """Store the latest product list shown to the user."""
        compact = [
            {
                "guid": p.get("guid", ""),
                "name": p.get("name", ""),
                "price": p.get("price", 0),
                "rest_abs": p.get("rest_abs", 0),
                "rest_rezerv": p.get("rest_rezerv", 0),
                "is_prescription": p.get("is_prescription", 0),
                "active_ingredient": p.get("active_ingredient", ""),
            }
            for p in products
        ]
        self._conn.execute("""
            INSERT INTO wa_sessions (chat_id, last_products_json, last_query,
                                     last_intent, page_offset, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_products_json=excluded.last_products_json,
                last_query=excluded.last_query,
                last_intent=excluded.last_intent,
                page_offset=excluded.page_offset,
                updated_at=excluded.updated_at
        """, (
            str(chat_id), json.dumps(compact, ensure_ascii=False),
            query, intent, page_offset, datetime.now().isoformat(),
        ))
        self._conn.commit()

    def get_list(self, chat_id: str) -> list[dict]:
        row = self._conn.execute(
            "SELECT last_products_json FROM wa_sessions WHERE chat_id=?",
            (str(chat_id),),
        ).fetchone()
        if not row or not row["last_products_json"]:
            return []
        try:
            return json.loads(row["last_products_json"])
        except json.JSONDecodeError:
            return []

    def get_session(self, chat_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM wa_sessions WHERE chat_id=?", (str(chat_id),),
        ).fetchone()
        return dict(row) if row else {}

    def set_current_product(self, chat_id: str, guid: str):
        self._conn.execute("""
            INSERT INTO wa_sessions (chat_id, current_product_guid, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                current_product_guid=excluded.current_product_guid,
                updated_at=excluded.updated_at
        """, (str(chat_id), guid, datetime.now().isoformat()))
        self._conn.commit()

    def get_current_product_guid(self, chat_id: str) -> str:
        row = self._conn.execute(
            "SELECT current_product_guid FROM wa_sessions WHERE chat_id=?",
            (str(chat_id),),
        ).fetchone()
        return row["current_product_guid"] if row else ""

    def clear(self, chat_id: str):
        self._conn.execute(
            "DELETE FROM wa_sessions WHERE chat_id=?", (str(chat_id),),
        )
        self._conn.commit()

    def cleanup_stale(self, max_age_days: int = 7):
        self._conn.execute(
            "DELETE FROM wa_sessions WHERE updated_at < datetime('now', ?)",
            (f"-{max_age_days} days",),
        )
        self._conn.commit()
