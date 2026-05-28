#!/usr/bin/env python3
"""Разовая загрузка коммуникаций по живым сделкам в sales_comms БД.

Запуск (на сервере):
  sudo -u crmbot venv/bin/python scripts/backfill_sales_comms.py --limit 100

Опции:
  --limit N        — макс. число сделок (default 500)
  --since YYYY-MM-DD — фильтр по DATE_CREATE >= (default: без фильтра, только активные)
  --include-closed   — включить WON/LOSE/JUNK (для варианта B — полный год)
  --dry-run        — только напечатать список целевых сделок, без записи

После прогона:
  • в deal_communications окажутся комменты, активити (звонки/задачи/письма),
    OL-сессии с сообщениями для каждой сделки;
  • звонки помечаются transcription_status='pending' — их потом догонит
    cron sales_comms_transcribe.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Импортироваться нужно из корня репозитория. На сервере скрипт запускается
# из /opt/crm-bot, sys.path уже включает корень — здесь подстраховка для
# локальных запусков.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from b24.client import Bitrix24Client
from db.connection import db
from sales_comms.collector import iter_active_deals, sync_deals_bulk
from sales_comms.db import stats as comms_stats

logger = structlog.get_logger()


async def main():
    parser = argparse.ArgumentParser(description="Backfill sales_comms DB")
    parser.add_argument("--limit", type=int, default=500, help="Max deals to process")
    parser.add_argument("--since", type=str, default=None, help="DATE_CREATE >= YYYY-MM-DD")
    parser.add_argument("--include-closed", action="store_true",
                        help="Не пропускать WON/LOSE/JUNK")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    await db.init()

    client = Bitrix24Client()
    try:
        print(f"[1/3] Fetching deals (limit={args.limit}, since={args.since}, include_closed={args.include_closed})…")
        t0 = time.time()
        if args.include_closed:
            # Прямой crm.deal.list без фильтрации по стадии. Для варианта B.
            params = {
                "select": ["ID", "TITLE", "STAGE_ID", "STATUS_SEMANTIC_ID", "ASSIGNED_BY_ID"],
                "order": {"DATE_CREATE": "DESC"},
            }
            if args.since:
                params["filter"] = {">=DATE_CREATE": args.since}
            resp = await client._call("crm.deal.list", params)
            deals = (resp or {}).get("result") or []
            deals = deals[: args.limit]
        else:
            # Только активные.
            deals = await iter_active_deals(client, max_items=args.limit)
            if args.since:
                deals = [d for d in deals if (d.get("DATE_CREATE") or "") >= args.since]

        print(f"   → {len(deals)} deals (in {round(time.time()-t0,1)}s)")
        if not deals:
            print("Nothing to do."); return

        ids = [int(d["ID"]) for d in deals if d.get("ID")]
        meta = {int(d["ID"]): d for d in deals if d.get("ID")}

        if args.dry_run:
            print("\nDry-run — these deals would be synced:")
            for d in deals[:30]:
                print(f"  • {d.get('ID')}  {d.get('STAGE_ID')}  {(d.get('TITLE') or '')[:60]}")
            if len(deals) > 30:
                print(f"  … +{len(deals)-30} more")
            return

        print(f"\n[2/3] Syncing {len(ids)} deals (≈{round(len(ids)*0.6,1)}s)…")
        t1 = time.time()
        processed, added, calls = await sync_deals_bulk(
            client, ids, deals_meta=meta, delay_between=0.3,
        )
        print(f"   → processed {processed} deals, added {added} comms, "
              f"queued {calls} calls for transcription "
              f"(in {round(time.time()-t1,1)}s)")

        print("\n[3/3] DB stats:")
        s = await comms_stats()
        print(f"   total records: {s['total']}")
        for t, n in sorted(s["by_type"].items()):
            print(f"     {t:18s} {n}")
        print(f"   pending transcriptions: {s['transcription_pending']}")
        print(f"   deals tracked: {s['deals_tracked']}")
    finally:
        try:
            await client.close()
        except Exception:
            pass
        try:
            await db.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
