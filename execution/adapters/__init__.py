"""Email + bank source adapters.

Each adapter conforms to a shared Protocol: ``source_id`` identity +
``fetch_since(watermark)`` generator + ``reauth()`` handler. Keeping
them behind a common protocol lets the orchestrator run multiple
adapters in parallel and aggregate their outcomes on the Run Status
sheet tab.
"""
