#!/usr/bin/env python3
"""Initialize database with schema and default settings."""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.connection import db
from db.repositories import settings as settings_repo, users as users_repo
from config import settings


async def init_database():
    """Initialize database."""
    print("Initializing database...")

    try:
        await db.init()
        print("✓ Database schema created")

        # Set default settings
        await settings_repo.set("default_model", "claude-sonnet-4-6")
        await settings_repo.set("routing_mode", "auto")
        print("✓ Default settings initialized")

        # Create admin user
        admin_id = settings.admin_telegram_id
        admin_exists = await users_repo.get_by_telegram_id(admin_id)

        if not admin_exists:
            await users_repo.create_user(
                telegram_id=admin_id,
                role="admin",
                b24_user_ids=[],
                display_name="Admin"
            )
            print(f"✓ Admin user created (ID: {admin_id})")
        else:
            print(f"✓ Admin user already exists (ID: {admin_id})")

        print("\n✅ Database initialization complete!")
        print(f"Database location: {settings.database_path}")

    except Exception as e:
        print(f"❌ Error initializing database: {e}")
        sys.exit(1)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(init_database())
