import asyncio
import websockets

async def test():
    try:
        async with websockets.connect('ws://localhost:8080/ws/media', subprotocols=['audio.twilio.com']) as ws:
            print('Connected!')
    except Exception as e:
        print(f'Error: {e}')

asyncio.run(test())
