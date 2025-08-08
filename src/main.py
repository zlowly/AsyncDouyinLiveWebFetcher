import asyncio

from liveroom import DoyinLiveRoom

if __name__ == "__main__":
    async def main():
        async with await DoyinLiveRoom.new("1234567890") as room:
            ws = await room.create_websocket()
            try:
                while True:
                    await asyncio.sleep(60)
            except KeyboardInterrupt:
                await ws.close()

    asyncio.run(main())
