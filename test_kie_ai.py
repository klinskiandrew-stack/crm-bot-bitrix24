#!/usr/bin/env python3
"""
Standalone test script for Kie.ai API connectivity.
Run: python test_kie_ai.py
"""

import asyncio
import json
from config import settings
from anthropic import AsyncAnthropic

async def test_kie_ai():
    print("=" * 80)
    print("🔍 KIE.AI API CONNECTIVITY TEST")
    print("=" * 80)

    # Check configuration
    print("\n📋 CONFIGURATION:")
    print(f"  API Key: {'✓' if settings.kie_api_key else '✗'} ({len(settings.kie_api_key)} chars)")
    print(f"  Base URL: {settings.kie_base_url}")
    print(f"  Proxy URL: {settings.kie_proxy_url or 'None'}")

    # Initialize client
    print("\n🔗 INITIALIZING CLIENT:")
    try:
        client = AsyncAnthropic(
            api_key=settings.kie_api_key,
            base_url=settings.kie_base_url,
            default_headers={
                "Authorization": f"Bearer {settings.kie_api_key}",
            },
        )
        print("  ✓ AsyncAnthropic client created")
    except Exception as e:
        print(f"  ✗ Failed to create client: {e}")
        return

    # Test 1: Simple message without tools
    print("\n📤 TEST 1: Simple message (no tools):")
    try:
        print("  Sending: 'Hello, Claude! What is 2+2?'")
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": "What is 2+2?"
                }
            ],
            system="You are a helpful assistant."
        )

        print(f"  ✓ Response received (HTTP 200)")
        print(f"    - Model: {response.model}")
        print(f"    - Stop reason: {response.stop_reason}")
        print(f"    - Content blocks: {len(response.content)}")

        if response.content:
            for i, block in enumerate(response.content):
                print(f"    - Block {i}: {type(block).__name__}")
                if hasattr(block, 'text'):
                    print(f"      Text: {block.text[:100]}...")
        else:
            print("    ✗ NO CONTENT BLOCKS RETURNED!")

        if response.usage:
            print(f"    - Usage: in={response.usage.input_tokens}, out={response.usage.output_tokens}")
        else:
            print(f"    - Usage: None")

    except Exception as e:
        print(f"  ✗ Failed: {type(e).__name__}: {e}")

    # Test 2: Message with empty system prompt
    print("\n📤 TEST 2: Message with empty system prompt:")
    try:
        print("  Sending: 'Compute 5*5' (no system prompt)")
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": "Compute 5*5"
                }
            ]
        )

        print(f"  ✓ Response received (HTTP 200)")
        print(f"    - Stop reason: {response.stop_reason}")
        print(f"    - Content blocks: {len(response.content)}")

        if response.content:
            for i, block in enumerate(response.content):
                if hasattr(block, 'text'):
                    print(f"    - Text: {block.text[:100]}...")
        else:
            print("    ✗ NO CONTENT BLOCKS!")

    except Exception as e:
        print(f"  ✗ Failed: {type(e).__name__}: {e}")

    # Test 3: Message with temperature variation
    print("\n📤 TEST 3: Message with temperature=1.0:")
    try:
        print("  Sending: 'Hello' with temperature=1.0")
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            temperature=1.0,
            messages=[
                {
                    "role": "user",
                    "content": "Say hello in a friendly way."
                }
            ]
        )

        print(f"  ✓ Response received")
        print(f"    - Stop reason: {response.stop_reason}")
        print(f"    - Content: {'✓ Present' if response.content else '✗ Empty'}")

    except Exception as e:
        print(f"  ✗ Failed: {type(e).__name__}: {e}")

    # Test 4: Batch test - multiple messages
    print("\n📤 TEST 4: Batch test (3 consecutive calls):")
    for i in range(1, 4):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=256,
                messages=[
                    {
                        "role": "user",
                        "content": f"Test message #{i}"
                    }
                ]
            )

            has_content = bool(response.content)
            tokens = (response.usage.input_tokens, response.usage.output_tokens) if response.usage else (0, 0)
            status = "✓" if has_content else "✗"
            print(f"  {status} Call {i}: stop_reason={response.stop_reason}, tokens={tokens}, has_content={has_content}")

        except Exception as e:
            print(f"  ✗ Call {i}: {type(e).__name__}: {str(e)[:60]}")

    # Test 5: Check raw response structure
    print("\n📤 TEST 5: Inspect raw response structure:")
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": "Simple test"
                }
            ]
        )

        print(f"  Response type: {type(response).__name__}")
        print(f"  Response attributes: {dir(response)[:10]}")  # First 10 attributes
        print(f"  Has 'id': {hasattr(response, 'id')}")
        print(f"  Has 'model': {hasattr(response, 'model')}")
        print(f"  Has 'content': {hasattr(response, 'content')}")
        print(f"  Has 'usage': {hasattr(response, 'usage')}")
        print(f"  Has 'stop_reason': {hasattr(response, 'stop_reason')}")

        if hasattr(response, 'content') and response.content:
            print(f"  Content[0] type: {type(response.content[0]).__name__}")
            print(f"  Content[0] has 'text': {hasattr(response.content[0], 'text')}")

    except Exception as e:
        print(f"  ✗ Failed: {e}")

    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(test_kie_ai())
