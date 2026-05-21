import aiosqlite
from pathlib import Path
from config import settings
import structlog

logger = structlog.get_logger()


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or settings.database_path
        self._connection = None

    async def init(self):
        """Initialize database and run migrations."""
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)

        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")

        await self._run_migrations()
        logger.info("Database initialized", db_path=self.db_path)

    async def _run_migrations(self):
        """Run SQL migrations from migrations.sql, then apply idempotent
        ALTER TABLE patches for existing databases."""
        migration_file = Path(__file__).parent / "migrations.sql"
        with open(migration_file) as f:
            migrations = f.read()

        await self._connection.executescript(migrations)

        # Patch existing databases — add new columns idempotently
        await self._ensure_column("users", "allow_private", "INTEGER DEFAULT 1")
        await self._ensure_column("lead_reports", "exported_at", "TIMESTAMP")
        # Stage 4 — CRM cross-link columns on lead_reports
        for col, definition in (
            ("b24_lead_id", "INTEGER"),
            ("b24_deal_id", "INTEGER"),
            ("crm_outcome", "TEXT"),
            ("crm_deal_stage", "TEXT"),
            ("crm_deal_result", "TEXT"),
            ("crm_deal_amount", "REAL"),
            ("crm_had_measurement", "TEXT"),
            ("crm_reason", "TEXT"),
            ("crm_manager_comment", "TEXT"),
            ("crm_card_url", "TEXT"),
            ("crm_synced_at", "TIMESTAMP"),
            ("notify_message_id", "INTEGER"),
        ):
            await self._ensure_column("lead_reports", col, definition)

        await self._connection.commit()

    async def _ensure_column(self, table: str, column: str, definition: str):
        """ALTER TABLE ADD COLUMN if column doesn't exist yet.
        SQLite has no 'IF NOT EXISTS' for columns — check via PRAGMA."""
        cursor = await self._connection.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        existing = {row[1] for row in rows}  # row[1] is column name
        if column not in existing:
            await self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )
            logger.info("Added column", table=table, column=column)

    async def close(self):
        """Close database connection."""
        if self._connection:
            await self._connection.close()

    async def execute(self, query: str, params=None):
        """Execute a query and return cursor."""
        if params is None:
            params = ()
        return await self._connection.execute(query, params)

    async def fetch_one(self, query: str, params=None):
        """Fetch a single row."""
        cursor = await self.execute(query, params)
        return await cursor.fetchone()

    async def fetch_all(self, query: str, params=None):
        """Fetch all rows."""
        cursor = await self.execute(query, params)
        return await cursor.fetchall()

    async def commit(self):
        """Commit changes."""
        await self._connection.commit()


# Global database instance
db = Database()
