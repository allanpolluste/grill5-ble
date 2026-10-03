#!/usr/bin/env python3
"""Command-line reader for a Grill5.0 BLE thermometer (via BlueZ D-Bus, no extra libraries).

Protocol (read from the Grill5.0 Android app, com.irainxun.grilltempsense 1.5.1):
  - device name starts with "Grill"
  - service 0xFFB0, write 0xFFB1, notify 0xFFB2
  - packet 55 00 + 6 x int16 (big-endian) = probes 1..6, value/10 = °C, -1 = no probe
  - realtime on: 55 1F 01 75 (last byte = sum of the previous bytes)

Usage:  python3 grill_cli.py [MAC]
"""
import sys
import time
import dbus
import dbus.mainloop.glib
from gi.repository import GLib

BLUEZ = 'org.bluez'
SVC = '0000ffb0-0000-1000-8000-00805f9b34fb'
WRITE = '0000ffb1-0000-1000-8000-00805f9b34fb'
NOTIFY = '0000ffb2-0000-1000-8000-00805f9b34fb'
OPEN_REALTIME = [0x55, 0x1F, 0x01, 0x75]

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
loop = GLib.MainLoop()
want_mac = sys.argv[1].upper() if len(sys.argv) > 1 else None
state = {'dev': None, 'last_data': 0.0, 'kicked': False}


def objects():
    om = dbus.Interface(bus.get_object(BLUEZ, '/'), 'org.freedesktop.DBus.ObjectManager')
    return om.GetManagedObjects()


def find_device():
    for path, ifs in objects().items():
        d = ifs.get('org.bluez.Device1')
        if not d:
            continue
        if want_mac and str(d.get('Address', '')).upper() == want_mac:
            return path
        if not want_mac and str(d.get('Name', '')).startswith('Grill'):
            return path
    return None


def find_char(dev, uuid):
    for path, ifs in objects().items():
        c = ifs.get('org.bluez.GattCharacteristic1')
        if c and path.startswith(dev + '/') and str(c['UUID']).lower() == uuid:
            return path
    return None


def decode(b):
    if len(b) < 2 or b[0] != 0x55:
        return None
    if b[1] == 0x00 and len(b) >= 14:
        out = []
        for i in range(6):
            v = int.from_bytes(bytes(b[2 + 2 * i:4 + 2 * i]), 'big', signed=True)
            out.append('  --  ' if v == -1 else f'{v / 10:5.1f}°')
        return '  '.join(f'S{i + 1}:{t}' for i, t in enumerate(out))
    return None


def on_value(iface, changed, invalidated, path=None):
    if 'Value' not in changed:
        return
    b = [int(x) for x in changed['Value']]
    state['last_data'] = time.time()
    txt = decode(b)
    stamp = time.strftime('%H:%M:%S')
    if txt:
        print(f'{stamp}  {txt}', flush=True)
    else:
        print(f'{stamp}  other packet: {" ".join(f"{x:02x}" for x in b)}', flush=True)


def kick_realtime(dev):
    # If nothing arrived within 5 s, send the app's "openRealtimeData" command.
    if state['last_data'] == 0 and not state['kicked']:
        state['kicked'] = True
        w = find_char(dev, WRITE)
        if w:
            print('No data yet, sending realtime command 55 1F 01 75 ...', flush=True)
            ch = dbus.Interface(bus.get_object(BLUEZ, w), 'org.bluez.GattCharacteristic1')
            ch.WriteValue(dbus.Array(OPEN_REALTIME, signature='y'), {})
    return False


def start(dev):
    print(f'Connecting: {dev}', flush=True)
    props = dbus.Interface(bus.get_object(BLUEZ, dev), 'org.freedesktop.DBus.Properties')
    device = dbus.Interface(bus.get_object(BLUEZ, dev), 'org.bluez.Device1')
    device.Connect()
    for _ in range(100):
        if props.Get('org.bluez.Device1', 'ServicesResolved'):
            break
        time.sleep(0.1)
    n = find_char(dev, NOTIFY)
    if not n:
        sys.exit('Characteristic FFB2 not found - is this really a Grill5.0 thermometer?')
    bus.add_signal_receiver(on_value, dbus_interface='org.freedesktop.DBus.Properties',
                            signal_name='PropertiesChanged', path=n)
    dbus.Interface(bus.get_object(BLUEZ, n), 'org.bluez.GattCharacteristic1').StartNotify()
    print('Listening for temperatures (Ctrl+C to stop)...', flush=True)
    GLib.timeout_add_seconds(5, kick_realtime, dev)


def main():
    adapter_path = next(p for p, i in objects().items() if 'org.bluez.Adapter1' in i)
    adapter = dbus.Interface(bus.get_object(BLUEZ, adapter_path), 'org.bluez.Adapter1')
    dev = find_device()
    if not dev:
        print('Searching for thermometer (name "Grill...")...', flush=True)
        adapter.StartDiscovery()
        for _ in range(30):
            time.sleep(1)
            dev = find_device()
            if dev:
                break
        try:
            adapter.StopDiscovery()
        except dbus.DBusException:
            pass
    if not dev:
        sys.exit('Thermometer not found. Is it switched on and the phone app closed?')
    state['dev'] = dev
    start(dev)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            dbus.Interface(bus.get_object(BLUEZ, dev), 'org.bluez.Device1').Disconnect()
        except dbus.DBusException:
            pass
        print('\nDisconnected.')


if __name__ == '__main__':
    main()
