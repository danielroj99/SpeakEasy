# bus.py - tiny in-process event bus (Nihal's design).
# One change: subscribe(..., inline=True) runs the handler in the publisher's
# thread. Use it for audio.frame so frames stay in order (a new thread per
# frame can deliver chunks out of order, which garbles Vosk).
import threading, collections

_subs = collections.defaultdict(list)

def subscribe(topic, fn, inline=False):
    _subs[topic].append((fn, inline))

def publish(topic, data=None):
    for fn, inline in list(_subs[topic]):
        if inline:
            fn(data)
        else:
            threading.Thread(target=fn, args=(data,), daemon=True).start()
