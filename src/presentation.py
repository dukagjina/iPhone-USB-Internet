"""Read-only presentation of worker counters. Never checks a phone or network."""
import math
import time


def number(value, default=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value if math.isfinite(value) and value >= 0 else default


def snapshot(status=None, state='stopped', available=False, now=None):
    status = status if isinstance(status, dict) else {}
    stats = status.get('stats', {})
    health = status.get('health', {})
    stats = stats if isinstance(stats, dict) else {}
    health = health if isinstance(health, dict) else {}
    now = time.time() if now is None else now
    updated = number(status.get('updated_at'))
    fresh = bool(available and state == 'connected' and status.get('state') == 'connected'
                 and 0 <= now - updated <= 5)
    result = {key: int(number(stats.get(key))) for key in
              ('active', 'capacity', 'queued', 'dial_waiting', 'rejected', 'failures',
               'peak_active', 'peak_waiting', 'dns_parked')}
    result['waiting'] = result['queued'] + result['dial_waiting']
    result['fresh'] = fresh
    result['recorded'] = bool(stats)
    result['state'] = state
    for key, threshold in (('usb', 15), ('internet', 65)):
        probe = health.get(key, {})
        probe = probe if isinstance(probe, dict) else {}
        age = number(probe.get('age_seconds'), None)
        latency = number(probe.get('latency_ms'), None)
        failures = int(number(probe.get('failures')))
        condition = ('inactive' if state != 'connected' else 'unknown' if not fresh else
                     'warning' if failures or (age is not None and age > threshold) else
                     'pending' if age is None else 'good')
        result[key] = dict(condition=condition, age=age, latency=latency, failures=failures)
    return result
