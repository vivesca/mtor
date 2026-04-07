"""Run the Temporal worker: python -m mtor.worker"""
from mtor.worker.translocase import main

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
