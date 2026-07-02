import asyncio
from backend.gpu_detector import detect_gpu_encoder

async def main():
    print(await detect_gpu_encoder())

asyncio.run(main())
