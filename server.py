import asyncio
import json
import math
import os
import re
import subprocess
import sys
import aiohttp
from aiohttp import web

# Set of connected WebSocket clients for the chat/control server
clients = set()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Vote trackers storing client WebSocket objects
votes = {
    'refresh': set(),
    'restartvm': set(),
    'revert': set()
}

# Key aliases mapping to xdotool key names
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


def normalize_key(k: str) -> str:
    """Normalize user input keys into xdotool compatible strings."""
    return KEY_MAP.get(k.lower(), k)


async def broadcast_sys(text: str):
    """Send a system message to all connected chat clients."""
    payload = json.dumps({'user': 'SYSTEM', 'message': text, 'isSystem': True})
    for client in list(clients):
        try:
            await client.send_str(payload)
        except Exception:
            pass


def get_qemu_cmd(disk_path: str) -> list:
    """Construct QEMU launch command with auto-detection for KVM hardware acceleration."""
    kvm_available = os.path.exists('/dev/kvm') and os.access('/dev/kvm', os.R_OK | os.W_OK)
    cpu_args = ["-accel", "kvm", "-cpu", "host"] if kvm_available else ["-accel", "tcg", "-cpu", "qemu64"]

    return [
        "qemu-system-x86_64",
        *cpu_args,
        "-m", "1024",
        "-smp", "2",
        "-vga", "cirrus",
        "-audiodev", "none,id=snd0",
        "-device", "ac97,audiodev=snd0",
        "-net", "nic,model=rtl8139",
        "-net", "user",
        "-boot", "c",
        "-monitor", "tcp:127.0.0.1:4444,server,nowait",
        "-drive", f"file={disk_path},format=qcow2,index=0,media=disk"
    ]


async def restart_qemu():
    """Kill current QEMU instance and restart it safely."""
    subprocess.run(["pkill", "-9", "-f", "qemu-system"], check=False)
    await asyncio.sleep(2)

    env = os.environ.copy()
    env["DISPLAY"] = ":1"
    disk_path = "/content/drive/MyDrive/winxp.qcow2"

    qemu_cmd = get_qemu_cmd(disk_path)
    subprocess.Popen(qemu_cmd, env=env)
    await broadcast_sys("💻 QEMU Virtual Machine successfully restarted!")


async def restart_script():
    """Restart python server process."""
    await broadcast_sys("🔄 Restarting application server process...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def check_vote(ws, user: str, vote_type: str, action_callback):
    """Process vote logic using 30% active connection threshold."""
    active_users = max(1, len(clients))
    threshold = math.ceil(active_users * 0.30)

    votes[vote_type].add(ws)
    current_votes = len(votes[vote_type])

    await broadcast_sys(f"🗳️ {user} voted to {vote_type}! ({current_votes}/{threshold} votes needed)")

    if current_votes >= threshold:
        votes[vote_type].clear()
        await broadcast_sys(f"✅ Vote threshold reached for !{vote_type}! Executing action...")
        await action_callback()


async def process_command(ws, user: str, text: str):
    """Parse and execute a single command using xdotool or system triggers."""
    parts = text.split(' ', 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ''

    env = os.environ.copy()
    env["DISPLAY"] = ":1"

    try:
        # --- VOTING & SYSTEM COMMANDS ---
        if cmd == '!refresh':
            await check_vote(ws, user, 'refresh', restart_script)
        elif cmd in ('!restartvm', '!revert'):
            await check_vote(ws, user, cmd[1:], restart_qemu)
        elif cmd == '!startvm':
            res = subprocess.run(["pgrep", "-f", "qemu-system"], capture_output=True)
            if not res.stdout:
                await restart_qemu()
            else:
                await broadcast_sys("VM is already running!")

        # --- TEXT INPUT (!type types string + presses Enter) ---
        elif cmd in ('!type', '!send'):
            if arg:
                subprocess.run(["xdotool", "type", "--delay", "50", arg], env=env)
                subprocess.run(["xdotool", "key", "Return"], env=env)

        # --- KEYBOARD COMMANDS ---
        elif cmd == '!key':
            if arg:
                k = normalize_key(arg)
                subprocess.run(["xdotool", "key", k], env=env)
        elif cmd == '!combo':
            if arg:
                keys = re.split(r'[\+\s]+', arg)
                norm_keys = "+".join([normalize_key(k) for k in keys if k])
                subprocess.run(["xdotool", "key", norm_keys], env=env)
        elif cmd == '!keydown':
            if arg:
                subprocess.run(["xdotool", "keydown", normalize_key(arg)], env=env)
        elif cmd == '!keyup':
            if arg:
                subprocess.run(["xdotool", "keyup", normalize_key(arg)], env=env)

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
                btn = "4" if val > 0 else "5"  # 4 = Up, 5 = Down
                subprocess.run(["xdotool", "click", "--repeat", str(abs(val)), btn], env=env)
        elif cmd == '!drag':
            subparts = arg.split()
            if len(subparts) >= 2:
                dx, dy = subparts[0], subparts[1]
                subprocess.run(["xdotool", "mousedown", "1", "mousemove_relative", "--", dx, dy, "mouseup", "1"], env=env)
        elif cmd == '!wait':
            if arg.isdigit():
                sec = min(int(arg), 5)
                await asyncio.sleep(sec)

    except Exception as e:
        print(f"Error processing command [{cmd}]:", e)


# --- ROUTE HANDLERS ---

async def handle_index(request):
    """Serve index.html web interface."""
    return web.FileResponse(os.path.join(BASE_DIR, 'index.html'))


async def handle_ws(request):
    """Handle chat WebSocket connection and multi-command parsing."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients.add(ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                user = data.get('user', 'Anonymous')
                text = data.get('message', '').strip()

                # Broadcast raw chat message to all connected clients
                payload = json.dumps({'user': user, 'message': text, 'isSystem': False})
                for client in list(clients):
                    try:
                        await client.send_str(payload)
                    except Exception:
                        pass

                # Parse and execute chained commands starting with '!'
                if '!' in text:
                    raw_cmds = [c.strip() for c in re.split(r'(?=\!)', text) if c.strip().startswith('!')]
                    for cmd_str in raw_cmds:
                        await process_command(ws, user, cmd_str)
    finally:
        clients.remove(ws)
        for key in votes:
            votes[key].discard(ws)
    return ws


async def handle_vnc_ws(request):
    """Proxy WebSocket traffic between client browser and local websockify/x11vnc."""
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


# --- APP SETUP ---

app = web.Application()
app.router.add_get('/', handle_index)
app.router.add_get('/ws', handle_ws)
app.router.add_get('/websockify', handle_vnc_ws)
app.router.add_static('/vnc/', path='/usr/share/novnc', name='novnc')

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=8000)
