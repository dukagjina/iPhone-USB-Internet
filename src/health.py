"""Health observations, not a promise of phone-only routing or internet reachability."""
import threading
import time


class Health:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.probes = {}

    def record(self, kind, ok, latency_ms=None):
        with self.lock:
            previous = self.probes.get(kind, {})
            self.probes[kind] = dict(last_ok=self.clock() if ok else previous.get('last_ok'),
                                    failures=0 if ok else previous.get('failures', 0) + 1,
                                    latency_ms=round(latency_ms, 1) if ok and latency_ms is not None else previous.get('latency_ms'))

    def snapshot(self):
        with self.lock:
            now = self.clock()
            return {kind: dict(age_seconds=round(now-item['last_ok'], 1) if item['last_ok'] is not None else None,
                               failures=item['failures'], latency_ms=item['latency_ms'])
                    for kind, item in self.probes.items()}


def describe(health, stats):
    usb, internet = health.get('usb', {}), health.get('internet', {})
    age = usb.get('age_seconds')
    usb_text = 'USB: checking' if age is None else ('USB: delayed' if usb.get('failures') or age > 15 else 'USB: responding')
    if age is not None:
        usb_text += f' ({age:.0f}s ago)'
        if usb.get('latency_ms') is not None:
            usb_text += f" / {usb['latency_ms']:.0f} ms USB response"
    age = internet.get('age_seconds')
    internet_text = 'HTTPS: not checked yet' if age is None else f'HTTPS: last passed {age:.0f}s ago'
    if internet.get('failures'):
        internet_text += ' — recent check failed'
    elif age is not None and age > 65:
        internet_text += ' — stale'
    busy = stats.get('queued', 0) + stats.get('dial_waiting', 0)
    warning = 'Capacity: waiting' if busy else ('Capacity: high usage' if stats.get('active', 0) >= .85 * max(1, stats.get('capacity', 0)) else 'Capacity: available')
    return usb_text + '  |  ' + internet_text + '\n' + warning
