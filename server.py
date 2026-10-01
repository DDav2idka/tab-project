import asyncio
import json
import os
import subprocess
import aiohttp
from aiohttp import web

clients = set()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

async def handle_index(request):
    return web.FileResponse(os.path.join(BASE_DIR, 'index.html'))

async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients.add(ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                user = data.get('user', 'Anonymous')
                text = data.get('message', '').strip()

                if text.startswith('!'):
                    await process_command(user, text)
                
                payload = json.dumps({'user': user, 'message': text, 'isSystem': False})
                for client in list(clients):
                    try:
                        await client.send_str(payload)
                    except Exception:
                        pass
    finally:
        clients.remove(ws)
    return ws

async def handle_vnc_ws(request):
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect('http://127.0.0.1:6080/websockify') as ws_server:
            async def forward_to_server():
                async for msg in ws_client:
                    if msg.type == web.WSMsgType.BINARY:
                        await ws_server.send_bytes(msg.data)
                    elif msg.type == web.WSMsgType.TEXT:
                        await ws_server.send_str(msg.data)

            async def forward_to_client():
                async for msg in ws_server:
                    if msg.type == web.WSMsgType.BINARY:
                        await ws_client.send_bytes(msg.data)
                    elif msg.type == web.WSMsgType.TEXT:
                        await ws_client.send_str(msg.data)

            await asyncio.gather(
                forward_to_server(),
                forward_to_client(),
                return_exceptions=True
            )
    return ws_client

async def process_command(user, text):
    parts = text.split(' ', 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ''

    env = os.environ.copy()
    env["DISPLAY"] = ":1"

    try:
        if cmd in ('!click', '!rclick'):
            coords = arg.split()
            if len(coords) >= 2:
                x, y = coords[0], coords[1]
                btn = '3' if cmd == '!rclick' else '1'
                subprocess.run(["xdotool", "mousemove", "--sync", x, y, "click", btn], env=env)
        elif cmd == '!type':
            subprocess.run(["xdotool", "type", "--delay", "50", arg], env=env)
        elif cmd == '!key':
            subprocess.run(["xdotool", "key", arg], env=env)
    except Exception as e:
        print("Command execution error:", e)

app = web.Application()
app.router.add_get('/', handle_index)
app.router.add_get('/ws', handle_ws)
app.router.add_get('/websockify', handle_vnc_ws)
app.router.add_static('/vnc/', path='/usr/share/novnc', name='novnc')

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=8000)