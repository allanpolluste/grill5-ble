#!/usr/bin/env python3
"""Local web page for a Grill5.0 BLE thermometer: live temperatures + chart.

Usage:  python3 grill_web.py [--port 8080] [--host 0.0.0.0] [--mac AA:BB:..] [--logs ~/grill_logs]
        (same settings via environment: GRILL_PORT, GRILL_HOST, GRILL_MAC, GRILL_LOG_DIR)
Open http://localhost:8080 in a browser (or http://<computer-IP>:8080 from a phone).
Readings are logged to <logs>/grill_<date>.csv; the last 24 h are reloaded on restart.

Protocol: see README.md (service FFB0, notify FFB2, packet 55 00 + 6 x int16 BE / 10).
"""
import argparse
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

BLUEZ = 'org.bluez'
NOTIFY = '0000ffb2-0000-1000-8000-00805f9b34fb'
PROBES = 6
HERE = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser()
ap.add_argument('--port', type=int, default=int(os.environ.get('GRILL_PORT', 8080)))
ap.add_argument('--host', default=os.environ.get('GRILL_HOST', '0.0.0.0'),
                help='0.0.0.0 = visible on the local network, 127.0.0.1 = this computer only')
ap.add_argument('--mac', default=os.environ.get('GRILL_MAC') or None,
                help='thermometer MAC (default: first device named "Grill*")')
ap.add_argument('--logs', default=os.environ.get('GRILL_LOG_DIR', '~/grill_logs'))
ap.add_argument('--config', default=os.environ.get('GRILL_CONFIG_DIR', '~/.config/grill'),
                help='where alarm limits are stored (alarms.json)')
args = ap.parse_args()

KEEP_SECONDS = 24 * 3600    # history kept in memory / shown on the chart

# ---------- shared state ----------
lock = threading.Lock()
history = []            # [(unix_ts, [t1..t6 or None])]
status = {'code': 'searching', 'state': 'starting', 'device': None, 'last': None}
clients = []            # SSE client queues

log_dir = os.path.expanduser(args.logs)
os.makedirs(log_dir, exist_ok=True)
log_state = {'day': None, 'file': None}
CSV_HEADER = 'time,' + ','.join(f'S{i + 1}' for i in range(PROBES)) + '\n'


def log_path_for(ts):
    return os.path.join(log_dir, time.strftime('grill_%Y-%m-%d.csv', time.localtime(ts)))


def write_log(ts, temps):
    """One CSV file per day; a new file is opened when the date changes."""
    day = time.strftime('%Y-%m-%d', time.localtime(ts))
    if log_state['day'] != day:
        if log_state['file']:
            log_state['file'].close()
        path = log_path_for(ts)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        log_state['file'] = open(path, 'a', buffering=1)
        if new:
            log_state['file'].write(CSV_HEADER)
        log_state['day'] = day
    log_state['file'].write(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) + ',' +
                            ','.join('' if t is None else f'{t:.1f}' for t in temps) + '\n')


def load_recent():
    """On restart, reload the last 24 h of readings from the CSV logs."""
    now = time.time()
    for path in sorted({log_path_for(now - KEEP_SECONDS), log_path_for(now)}):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                parts = line.strip().split(',')
                if len(parts) != PROBES + 1 or parts[0] == 'time':
                    continue
                try:
                    t = time.mktime(time.strptime(parts[0], '%Y-%m-%d %H:%M:%S'))
                    temps = [float(x) if x else None for x in parts[1:]]
                except ValueError:
                    continue
                if t >= now - KEEP_SECONDS:
                    history.append((t, temps))
    history.sort(key=lambda r: r[0])


load_recent()


def broadcast(msg):
    data = json.dumps(msg)
    with lock:
        for q in list(clients):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


def set_state(code, state, device=None):
    """code: searching | connecting | connected | lost | error (drives the page's status colour)."""
    with lock:
        status['code'] = code
        status['state'] = state
        if device:
            status['device'] = device
    broadcast({'type': 'status', **status})
    print(f'[{time.strftime("%H:%M:%S")}] {state}', flush=True)


# ---------- per-probe min/max alarms ----------
# A probe alarms when it leaves [min, max]. Muting silences it until the reading has come back
# inside the range (by at least HYSTERESIS, so jitter at the limit doesn't count) - after that
# the next exit alarms again.
HYSTERESIS = 1.0
config_dir = os.path.expanduser(args.config)
os.makedirs(config_dir, exist_ok=True)
limits_path = os.path.join(config_dir, 'alarms.json')
alarms = [{'min': None, 'max': None, 'state': 'ok', 'muted': False} for _ in range(PROBES)]


def load_limits():
    try:
        with open(limits_path) as f:
            saved = json.load(f)
        for a, s in zip(alarms, saved):
            a['min'], a['max'] = s.get('min'), s.get('max')
    except (OSError, ValueError):
        pass


def save_limits():
    tmp = limits_path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump([{'min': a['min'], 'max': a['max']} for a in alarms], f, indent=1)
    os.replace(tmp, limits_path)


def outside(a, v):
    if a['max'] is not None and v > a['max']:
        return 'high'
    if a['min'] is not None and v < a['min']:
        return 'low'
    return None


def back_inside(a, v):
    lo, hi = a['min'], a['max']
    margin = HYSTERESIS
    if lo is not None and hi is not None:
        margin = min(margin, (hi - lo) / 4)
    return (lo is None or v >= lo + margin) and (hi is None or v <= hi - margin)


def evaluate(i, v):
    """Update alarm i for reading v. Returns True if anything changed. Caller holds lock."""
    a = alarms[i]
    before = (a['state'], a['muted'])
    if v is None or (a['min'] is None and a['max'] is None):
        a['state'], a['muted'] = 'ok', False
    else:
        out = outside(a, v)
        if a['state'] == 'ok':
            if out:
                a['state'], a['muted'] = out, False
        elif out and out != a['state']:          # jumped straight across to the other side
            a['state'], a['muted'] = out, False
        elif not out and back_inside(a, v):      # back in range -> re-arm
            a['state'], a['muted'] = 'ok', False
    if (a['state'], a['muted']) != before:
        print(f'[{time.strftime("%H:%M:%S")}] probe {i + 1}: '
              f'{"ALARM " + a["state"] if a["state"] != "ok" else "back in range"} ({v})', flush=True)
        return True
    return False


def alarms_msg():
    return {'type': 'alarms', 'alarms': [dict(a) for a in alarms]}


def latest(i):
    for _, temps in reversed(history):
        return temps[i]
    return None


load_limits()


def add_reading(temps):
    ts = time.time()
    with lock:
        history.append((ts, temps))
        status['last'] = ts
        cut = 0
        while cut < len(history) and history[cut][0] < ts - KEEP_SECONDS:
            cut += 1
        if cut:
            del history[:cut]
        changed = [evaluate(i, t) for i, t in enumerate(temps)]
    write_log(ts, temps)
    broadcast({'type': 'reading', 't': ts, 'v': temps})
    if any(changed):
        broadcast(alarms_msg())


# ---------- Bluetooth (BlueZ over D-Bus, GLib main loop) ----------
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
ble = {'dev': None, 'char': None, 'busy': False, 'listening': set()}


def objects():
    om = dbus.Interface(bus.get_object(BLUEZ, '/'), 'org.freedesktop.DBus.ObjectManager')
    return om.GetManagedObjects()


def adapter():
    path = next(p for p, i in objects().items() if 'org.bluez.Adapter1' in i)
    return dbus.Interface(bus.get_object(BLUEZ, path), 'org.bluez.Adapter1')


def find_device():
    for path, ifs in objects().items():
        d = ifs.get('org.bluez.Device1')
        if not d:
            continue
        if args.mac and str(d.get('Address', '')).upper() == args.mac.upper():
            return path, str(d.get('Name', d.get('Address')))
        if not args.mac and str(d.get('Name', '')).startswith('Grill'):
            return path, str(d['Name'])
    return None, None


def find_char(dev, uuid):
    for path, ifs in objects().items():
        c = ifs.get('org.bluez.GattCharacteristic1')
        if c and path.startswith(dev + '/') and str(c['UUID']).lower() == uuid:
            return path
    return None


def on_value(iface, changed, invalidated):
    if 'Value' not in changed:
        return
    b = bytes(int(x) for x in changed['Value'])
    if len(b) >= 2 + 2 * PROBES and b[0] == 0x55 and b[1] == 0x00:
        temps = []
        for i in range(PROBES):
            v = int.from_bytes(b[2 + 2 * i:4 + 2 * i], 'big', signed=True)
            temps.append(None if v == -1 else v / 10)
        add_reading(temps)


def lost(reason):
    if not ble['char']:
        return
    set_state('lost', f'{reason}, retrying')
    ble['char'] = None
    if ble['dev']:
        try:
            dbus.Interface(bus.get_object(BLUEZ, ble['dev']), 'org.bluez.Device1').Disconnect()
        except dbus.DBusException:
            pass
    GLib.timeout_add_seconds(3, try_connect)


def on_device_props(iface, changed, invalidated):
    if 'Connected' in changed and not bool(changed['Connected']):
        lost('connection lost')


def watchdog():
    # Safety net in case the disconnect signal never arrives: 30 s without data = reconnect.
    if ble['char'] and status['last'] and time.time() - status['last'] > 30:
        lost('no data')
    return True


def try_connect():
    """Find the thermometer, connect and enable notifications. Returns True = try again later."""
    if ble['busy'] or ble['char']:
        return False
    ble['busy'] = True
    try:
        dev, name = find_device()
        if not dev:
            set_state('searching', 'searching for thermometer')
            try:
                adapter().StartDiscovery()
            except dbus.DBusException:
                pass
            return True
        try:
            adapter().StopDiscovery()
        except dbus.DBusException:
            pass
        set_state('connecting', 'connecting', name)
        props = dbus.Interface(bus.get_object(BLUEZ, dev), 'org.freedesktop.DBus.Properties')
        if ble['dev'] != dev:
            bus.add_signal_receiver(on_device_props, dbus_interface='org.freedesktop.DBus.Properties',
                                    signal_name='PropertiesChanged', path=dev)
            ble['dev'] = dev
        dbus.Interface(bus.get_object(BLUEZ, dev), 'org.bluez.Device1').Connect()
        for _ in range(100):
            if props.Get('org.bluez.Device1', 'ServicesResolved'):
                break
            time.sleep(0.1)
        ch = find_char(dev, NOTIFY)
        if not ch:
            set_state('error', 'characteristic FFB2 not found')
            return True
        if ch not in ble['listening']:
            bus.add_signal_receiver(on_value, dbus_interface='org.freedesktop.DBus.Properties',
                                    signal_name='PropertiesChanged', path=ch)
            ble['listening'].add(ch)
        dbus.Interface(bus.get_object(BLUEZ, ch), 'org.bluez.GattCharacteristic1').StartNotify()
        ble['char'] = ch
        set_state('connected', 'connected', name)
        return False
    except dbus.DBusException as e:
        set_state('error', f'error: {e.get_dbus_name()}, retrying')
        return True
    finally:
        ble['busy'] = False


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            with open(os.path.join(HERE, 'grill_web.html'), 'rb') as f:
                self.send(200, 'text/html; charset=utf-8', f.read())
        elif self.path == '/api/history':
            with lock:
                body = json.dumps({'status': status, 'history': history,
                                   'alarms': alarms_msg()['alarms']})
            self.send(200, 'application/json', body.encode())
        elif self.path == '/api/stream':
            q = queue.Queue(maxsize=1000)
            with lock:
                clients.append(q)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f'data: {data}\n\n'.encode())
                    except queue.Empty:
                        self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    clients.remove(q)
        else:
            self.send(404, 'text/plain', b'404')

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length', 0))
            req = json.loads(self.rfile.read(min(n, 4096)) or b'{}')
        except ValueError:
            return self.send(400, 'text/plain', b'bad json')
        if self.path == '/api/limits':
            # {"probe": 1..6, "min": number|null, "max": number|null}
            try:
                i = int(req['probe']) - 1
                lo = None if req.get('min') in (None, '') else float(req['min'])
                hi = None if req.get('max') in (None, '') else float(req['max'])
            except (KeyError, TypeError, ValueError):
                return self.send(400, 'text/plain', b'expected probe, min, max')
            if not 0 <= i < PROBES:
                return self.send(400, 'text/plain', b'probe must be 1..6')
            if any(x is not None and not -50 <= x <= 500 for x in (lo, hi)):
                return self.send(400, 'text/plain', b'limits must be between -50 and 500')
            if lo is not None and hi is not None and lo >= hi:
                return self.send(400, 'text/plain', b'min must be below max')
            with lock:
                a = alarms[i]
                a['min'], a['max'] = lo, hi
                a['state'], a['muted'] = 'ok', False     # new limits: start fresh
                evaluate(i, latest(i))
                save_limits()
                msg = alarms_msg()
            print(f'[{time.strftime("%H:%M:%S")}] probe {i + 1}: limits {lo}..{hi}', flush=True)
        elif self.path == '/api/mute':
            # {"probe": 1..6} or {"all": true}
            with lock:
                targets = range(PROBES) if req.get('all') else [int(req.get('probe', 0)) - 1]
                for i in targets:
                    if 0 <= i < PROBES and alarms[i]['state'] != 'ok':
                        alarms[i]['muted'] = True
                msg = alarms_msg()
        else:
            return self.send(404, 'text/plain', b'404')
        broadcast(msg)
        self.send(200, 'application/json', json.dumps(msg).encode())


def main():
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f'Web page: http://{args.host}:{args.port}   (logs: {log_dir}, {len(history)} readings loaded)', flush=True)
    loop = GLib.MainLoop()
    if try_connect():
        GLib.timeout_add_seconds(5, try_connect)
    GLib.timeout_add_seconds(10, watchdog)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        if ble['dev']:
            try:
                dbus.Interface(bus.get_object(BLUEZ, ble['dev']), 'org.bluez.Device1').Disconnect()
            except dbus.DBusException:
                pass
        print('\nDisconnected.')


if __name__ == '__main__':
    main()
