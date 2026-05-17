#!/usr/bin/env python3
"""
Standalone test script for Bitrix24 API connectivity.
Run: python test_bitrix24.py
"""

import asyncio
import aiohttp
import json
from config import settings

async def test_bitrix24():
    print("=" * 80)
    print("🔍 BITRIX24 API CONNECTIVITY TEST")
    print("=" * 80)

    # Check configuration
    print("\n📋 CONFIGURATION:")
    print(f"  Webhook URL: {'✓' if settings.b24_webhook_url else '✗'}")
    if settings.b24_webhook_url:
        url_preview = settings.b24_webhook_url[:80] + "..." if len(settings.b24_webhook_url) > 80 else settings.b24_webhook_url
        print(f"    {url_preview}")

    if not settings.b24_webhook_url:
        print("  ✗ B24_WEBHOOK_URL not configured in .env")
        return

    # Helper function to make API calls
    async def b24_call(method: str, params: dict = None):
        """Make authenticated call to Bitrix24 API"""
        url = f"{settings.b24_webhook_url}{method}"
        payload = params or {}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    return await resp.json(), resp.status
        except asyncio.TimeoutError:
            return {"error": "Request timeout"}, 0
        except Exception as e:
            return {"error": str(e)}, 0

    # Test 1: Check B24 availability
    print("\n📤 TEST 1: Check Bitrix24 availability (user.get):")
    result, status = await b24_call("user.get")

    if status == 200:
        print(f"  ✓ HTTP {status}")
        if "error" in result:
            print(f"    Error: {result['error']}")
        elif "result" in result:
            user_data = result["result"]
            print(f"    User ID: {user_data.get('ID')}")
            print(f"    User name: {user_data.get('NAME')} {user_data.get('LAST_NAME')}")
        print(f"    Full response keys: {list(result.keys())}")
    else:
        print(f"  ✗ HTTP {status}")
        print(f"    Response: {result}")

    # Test 2: Get deals
    print("\n📤 TEST 2: Get deals (crm.deal.list):")
    result, status = await b24_call("crm.deal.list", {
        "filter": {},
        "select": ["ID", "TITLE", "STAGE_ID"],
        "limit": 5
    })

    if status == 200:
        print(f"  ✓ HTTP {status}")
        if "error" in result:
            print(f"    Error: {result['error']}")
        elif "result" in result:
            deals = result["result"]
            print(f"    Deals found: {len(deals)}")
            for i, deal in enumerate(deals[:3], 1):
                print(f"    [{i}] ID={deal.get('ID')}, Title={deal.get('TITLE', 'N/A')}, Stage={deal.get('STAGE_ID', 'N/A')}")
        if "total" in result:
            print(f"    Total: {result['total']}")
    else:
        print(f"  ✗ HTTP {status}: {result}")

    # Test 3: Get leads
    print("\n📤 TEST 3: Get leads (crm.lead.list):")
    result, status = await b24_call("crm.lead.list", {
        "filter": {},
        "select": ["ID", "TITLE", "STATUS_ID"],
        "limit": 5
    })

    if status == 200:
        print(f"  ✓ HTTP {status}")
        if "result" in result:
            leads = result["result"]
            print(f"    Leads found: {len(leads)}")
            for i, lead in enumerate(leads[:3], 1):
                print(f"    [{i}] ID={lead.get('ID')}, Title={lead.get('TITLE', 'N/A')}")
        if "error" in result:
            print(f"    Error: {result['error']}")
    else:
        print(f"  ✗ HTTP {status}: {result}")

    # Test 4: Search contacts
    print("\n📤 TEST 4: Search contacts (crm.contact.list):")
    result, status = await b24_call("crm.contact.list", {
        "filter": {},
        "select": ["ID", "NAME", "PHONE"],
        "limit": 5
    })

    if status == 200:
        print(f"  ✓ HTTP {status}")
        if "result" in result:
            contacts = result["result"]
            print(f"    Contacts found: {len(contacts)}")
            for i, contact in enumerate(contacts[:3], 1):
                phones = contact.get('PHONE', [])
                phone_str = phones[0] if isinstance(phones, list) and phones else "N/A"
                print(f"    [{i}] ID={contact.get('ID')}, Name={contact.get('NAME', 'N/A')}, Phone={phone_str}")
        if "error" in result:
            print(f"    Error: {result['error']}")
    else:
        print(f"  ✗ HTTP {status}: {result}")

    # Test 5: Get companies
    print("\n📤 TEST 5: Get companies (crm.company.list):")
    result, status = await b24_call("crm.company.list", {
        "filter": {},
        "select": ["ID", "TITLE"],
        "limit": 5
    })

    if status == 200:
        print(f"  ✓ HTTP {status}")
        if "result" in result:
            companies = result["result"]
            print(f"    Companies found: {len(companies)}")
            for i, company in enumerate(companies[:3], 1):
                print(f"    [{i}] ID={company.get('ID')}, Title={company.get('TITLE', 'N/A')}")
        if "error" in result:
            print(f"    Error: {result['error']}")
    else:
        print(f"  ✗ HTTP {status}: {result}")

    # Test 6: Get activities
    print("\n📤 TEST 6: Get activities (crm.activity.list):")
    result, status = await b24_call("crm.activity.list", {
        "filter": {},
        "select": ["ID", "SUBJECT", "ACTIVITY_DATE"],
        "limit": 5
    })

    if status == 200:
        print(f"  ✓ HTTP {status}")
        if "result" in result:
            activities = result["result"]
            print(f"    Activities found: {len(activities)}")
            for i, activity in enumerate(activities[:3], 1):
                print(f"    [{i}] ID={activity.get('ID')}, Subject={activity.get('SUBJECT', 'N/A')}")
        if "error" in result:
            print(f"    Error: {result['error']}")
    else:
        print(f"  ✗ HTTP {status}: {result}")

    # Test 7: Test with filtering
    print("\n📤 TEST 7: Test deal filtering:")
    result, status = await b24_call("crm.deal.list", {
        "filter": {"STAGE_ID": "C2:NEW"},
        "select": ["ID", "TITLE", "STAGE_ID"],
        "limit": 3
    })

    if status == 200:
        print(f"  ✓ HTTP {status}")
        if "result" in result:
            deals = result["result"]
            print(f"    Deals in 'NEW' stage: {len(deals)}")
        else:
            print(f"    Error: {result.get('error', 'Unknown error')}")
    else:
        print(f"  ✗ HTTP {status}")

    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(test_bitrix24())
