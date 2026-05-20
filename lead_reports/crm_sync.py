"""Daily CRM refresh for collected lead calls.

Re-pulls Bitrix state for leads that are still 'live' (no final outcome
yet), then fully redraws the Google Sheet. Closed leads — Неквал or a
finished deal — never change, so they're skipped.

Wired into the report scheduler to run once a day.
"""

import structlog

from b24.client import Bitrix24Client
from lead_reports import crm_enricher, lead_db, sheets_exporter

logger = structlog.get_logger()


async def crm_refresh() -> dict:
    """Refresh CRM data for live leads and redraw the sheet."""
    leads = await lead_db.get_for_crm_sync(limit=2000)
    logger.info("CRM refresh started", to_sync=len(leads))

    client = Bitrix24Client()
    await client._ensure_session()
    synced = 0
    try:
        for lead in leads:
            try:
                crm = await crm_enricher.enrich(lead, client)
                await lead_db.update_crm(lead["id"], crm)
                synced += 1
            except Exception as e:
                logger.error("CRM refresh — lead failed", lead_id=lead.get("id"), error=str(e))
    finally:
        try:
            await client.close()
        except Exception:
            pass

    rebuild = await sheets_exporter.rebuild_sheet()
    result = {"synced": synced, "rebuilt": rebuild.get("rebuilt", 0)}
    logger.info("CRM refresh finished", **result)
    return result
