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
        """Run SQL migrations from migrations.sql."""
        migration_file = Path(__file__).parent / "migrations.sql"
        with open(migration_file) as f:
            migrations = f.read()

        await self._connection.executescript(migrations)
        await self._connection.commit()

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
