import asyncio
import json
import math
import os
import re
import subprocess
import sys
import aiohttp
from aiohttp import web

clients = set()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Voting trackers: stores set of client websocket objects
votes = {
    'refresh': set(),
    'restartvm': set(),
    'revert': set()
}

# Key aliases map to xdotool key names
KEY_MAP = {
    'ctrl': 'Control_L', 'lctrl': 'Control_L', 'rctrl': 'Control_R',
    'alt': 'Alt_L', 'lalt': 'Alt_L', 'ralt': 'Alt_R',
    'shift': 'Shift_L', 'lshift': 'Shift_L', 'rshift': 'Shift_R',
    'win': 'Super_L', 'lwin': 'Super_L', 'rwin': 'Super_R',
    'enter': 'Return', 'esc': 'Escape', 'tab': 'Tab', 'space': 'space',
    'backspace': 'BackSpace', 'del': 'Delete', 'delete': 'Delete',
    'home': 'Home', 'end': 'End', 'pageup': 'Page_Up', 'pagedown': 'Page_Down',
    'insert': 'Insert', 'menu': 'Menu', 'scrolllock': 'Scroll_Lock',
    'numlock': 'Num_Lock', 'capslock': 'Caps_Lock',
    'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
}
for i in range(1, 13):
    KEY_MAP[f'f{i}'] = f'F{i}'

def normalize_key(k):
    k_lower = k.lower()
    return KEY_MAP.get(k_lower, k)

async def broadcast_sys(text):
    payload = json.dumps({'user': 'SYSTEM', 'message': text, 'isSystem': True})
    for client in list(clients):
        try:
            await client.send_str(payload)
        except Exception:
            pass

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

                # Broadcast chat message to all
                payload = json.dumps({'user': user, 'message': text, 'isSystem': False})
                for client in list(clients):
                    try:
                        await client.send_str(payload)
                    except Exception:
                        pass

                # Handle Commands
                if text.startswith('!'):
                    await process_command(ws, user, text)
    finally:
        clients.remove(ws)
        # Clean up stale votes when user disconnects
        for key in votes:
            votes[key].discard(ws)
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

            await asyncio.gather(forward_to_server(), forward_to_client(), return_exceptions=True)
    return ws_client

async def check_vote(ws, user, vote_type, action_callback):
    active_users = max(1, len(clients))
    threshold = math.ceil(active_users * 0.30)
    
    votes[vote_type].add(ws)
    current_votes = len(votes[vote_type])

    await broadcast_sys(f"🗳️ {user} voted to {vote_type}! ({current_votes}/{threshold} votes needed)")

    if current_votes >= threshold:
        votes[vote_type].clear()
        await broadcast_sys(f"✅ Vote threshold reached for !{vote_type}! Executing action...")
        await action_callback()

async def restart_qemu():
    subprocess.run(["pkill", "-9", "-f", "qemu-system"], check=False)
    await asyncio.sleep(2)
    env = os.environ.copy()
    env["DISPLAY"] = ":1"
    disk_path = "/content/drive/MyDrive/winxp.qcow2"
    qemu_cmd = [
        "qemu-system-x86_64",
        "-accel", "kvm:tcg",
        "-cpu", "host,qemu64",
        "-m", "1024",
        "-smp", "2",
        "-vga", "std",
        "-audiodev", "none,id=snd0",
        "-device", "ac97,audiodev=snd0",
        "-net", "nic,model=rtl8139",
        "-net", "user",
        "-boot", "c",
        "-monitor", "tcp:127.0.0.1:4444,server,nowait",
        "-drive", f"file={disk_path},format=qcow2,index=0,media=disk",
        "-drive", "if=ide,index=1,media=cdrom,id=cd0"
    ]
    subprocess.Popen(qemu_cmd, env=env)
    await broadcast_sys("💻 QEMU VM successfully restarted!")

async def restart_script():
    await broadcast_sys("🔄 Restarting script environment...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def process_command(ws, user, text):
    parts = text.split(' ', 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ''

    env = os.environ.copy()
    env["DISPLAY"] = ":1"

    try:
        # --- VOTING COMMANDS ---
        if cmd == '!refresh':
            await check_vote(ws, user, 'refresh', restart_script)
        elif cmd == '!restartvm':
            await check_vote(ws, user, 'restartvm', restart_qemu)
        elif cmd == '!revert':
            await check_vote(ws, user, 'revert', restart_qemu)
        elif cmd == '!startvm':
            # Check if QEMU running
            res = subprocess.run(["pgrep", "-f", "qemu-system"], capture_output=True)
            if not res.stdout:
                await restart_qemu()
            else:
                await broadcast_sys("VM is already running!")

        # --- TEXT COMMANDS ---
        elif cmd == '!type':
            if arg:
                subprocess.run(["xdotool", "type", "--delay", "50", arg], env=env)
        elif cmd == '!send':
            if arg:
                subprocess.run(["xdotool", "type", "--delay", "50", arg], env=env)
                subprocess.run(["xdotool", "key", "Return"], env=env)

        # --- KEY COMMANDS ---
        elif cmd == '!key':
            if arg:
                k = normalize_key(arg)
                subprocess.run(["xdotool", "key", k], env=env)
        elif cmd == '!combo':
            if arg:
                # Handle ctrl+c or ctrl c
                keys = re.split(r'[\+\s]+', arg)
                norm_keys = "+".join([normalize_key(k) for k in keys if k])
                subprocess.run(["xdotool", "key", norm_keys], env=env)
        elif cmd == '!keydown':
            if arg:
                k = normalize_key(arg)
                subprocess.run(["xdotool", "keydown", k], env=env)
        elif cmd == '!keyup':
            if arg:
                k = normalize_key(arg)
                subprocess.run(["xdotool", "keyup", k], env=env)

        # --- MOUSE COMMANDS ---
        elif cmd == '!move':
            subparts = arg.split()
            if len(subparts) >= 2:
                direction, dist = subparts[0].lower(), int(subparts[1])
                dx, dy = 0, 0
                if direction == 'right': dx = dist
                elif direction == 'left': dx = -dist
                elif direction == 'down': dy = dist
                elif direction == 'up': dy = -dist
                subprocess.run(["xdotool", "mousemove_relative", "--", str(dx), str(dy)], env=env)
        elif cmd == '!abs':
            subparts = arg.split()
            if len(subparts) >= 2:
                x, y = subparts[0], subparts[1]
                subprocess.run(["xdotool", "mousemove", x, y], env=env)
        elif cmd == '!click':
            repeat = arg if arg.isdigit() else '1'
            subprocess.run(["xdotool", "click", "--repeat", repeat, "1"], env=env)
        elif cmd == '!rclick':
            subprocess.run(["xdotool", "click", "3"], env=env)
        elif cmd == '!mclick':
            subprocess.run(["xdotool", "click", "2"], env=env)
        elif cmd == '!scroll':
            if arg.lstrip('-').isdigit():
                val = int(arg)
                btn = "4" if val > 0 else "5" # 4 = Up, 5 = Down
                subprocess.run(["xdotool", "click", "--repeat", str(abs(val)), btn], env=env)
        elif cmd == '!drag':
            subparts = arg.split()
            if len(subparts) >= 2:
                dx, dy = subparts[0], subparts[1]
                subprocess.run(["xdotool", "mousedown", "1", "mousemove_relative", "--", dx, dy, "mouseup", "1"], env=env)
        elif cmd == '!wait':
            if arg.isdigit():
                sec = min(int(arg), 5) # Max 5 sec limit to prevent freezing
                await asyncio.sleep(sec)

    except Exception as e:
        print("Error executing command:", e)

app = web.Application()
app.router.add_get('/', handle_index)
app.router.add_get('/ws', handle_ws)
app.router.add_get('/websockify', handle_vnc_ws)
app.router.add_static('/vnc/', path='/usr/share/novnc', name='novnc')

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=8000)
