#!/usr/bin/env python3
"""Health check script for the bot - can be run via cron."""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.connection import db
from db.repositories import audit


async def health_check():
    """Check bot health and report issues."""
    await db.init()

    print(f"\n🏥 Health Check - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Check database
    print("\n📊 Database Status:")
    try:
        await db.execute("SELECT 1")
        print("  ✅ Database connection: OK")
    except Exception as e:
        print(f"  ❌ Database connection: FAILED ({e})")
        return False

    # Check recent errors
    print("\n⚠️  Recent Errors (last 24h):")
    errors = await audit.get_recent_errors(limit=10)
    if errors:
        for err in errors[:5]:
            print(f"  ❌ {err['created_at']}: {err['error'][:60]}...")
        print(f"  Total errors: {len(errors)}")
    else:
        print("  ✅ No errors found")

    # Check activity
    print("\n📈 Activity (last 24h):")
    stats = await audit.get_stats(days=1)
    requests = stats.get('total_requests', 0)
    credits = stats.get('total_credits', 0)

    if requests > 0:
        print(f"  ✅ Requests: {requests}")
        print(f"  💰 Credits spent: {credits:.2f}")
        avg_tokens = stats.get('avg_input_tokens', 0)
        print(f"  📝 Avg input tokens: {avg_tokens}")
    else:
        print("  ℹ️  No requests in last 24h")

    # Check database size
    print("\n💾 Database Size:")
    db_path = Path("./data/bot.sqlite")
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        print(f"  Database: {size_mb:.2f} MB")
        if size_mb > 100:
            print(f"  ⚠️  Database is getting large, consider cleanup")
    else:
        print(f"  ❌ Database file not found: {db_path}")

    # Check audit log size
    print("\n📋 Audit Log:")
    audit_stats = await db.fetch_one(
        "SELECT COUNT(*) as count FROM audit_log"
    )
    if audit_stats:
        count = audit_stats['count']
        print(f"  Records: {count}")
        if count > 100000:
            print(f"  ⚠️  Audit log is large, cleanup recommended")

    # Overall status
    print("\n" + "=" * 60)
    if requests > 0 or len(errors) == 0:
        print("✅ Overall Status: HEALTHY")
        status = True
    else:
        print("⚠️  Overall Status: CHECK NEEDED")
        status = True  # Not a critical failure

    await db.close()
    return status


if __name__ == "__main__":
    try:
        result = asyncio.run(health_check())
        sys.exit(0 if result else 1)
    except Exception as e:
        print(f"\n❌ Health check failed: {e}")
        sys.exit(1)
