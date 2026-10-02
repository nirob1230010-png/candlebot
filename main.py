"""
Next Candle Prediction Trading Bot  (Kivy 2.3.0, single file, no .kv)
Crypto: Binance WebSocket | Forex: TwelveData REST
Indicators are pure Python (no numpy/pandas). Signals are indicators, not advice.
"""
import os
import ssl
import json
import math
import time
import asyncio
import calendar
import threading
from collections import deque

import requests

from kivy.app import App
from kivy.clock import Clock
from kivy.core.audio import SoundLoader
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.widget import Widget
from kivy.utils import platform

try:
    from plyer import tts
except Exception:  # plyer/pyjnius missing -> silent mode
    tts = None

# ----------------------------------------------------------------- config
API_KEY = os.environ.get("TWELVEDATA_API_KEY", "YOUR_TWELVEDATA_KEY")  # env vars don't exist on Android: edit this
TD_URL = "https://api.twelvedata.com/time_series"
CRYPTO = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT"]
FOREX = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "EUR/JPY", "GBP/JPY"]
INTERVALS = ["1m", "5m", "15m", "30m", "1h"]
IV_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
IV_SPOKEN = {"1m": "1 minute", "5m": "5 minutes", "15m": "15 minutes", "30m": "30 minutes", "1h": "1 hour"}
TD_IV = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}
MAX_CANDLES = 150
CHART_N = 30
HEADER_ICON = "⚡ "  # if you see a square box on your phone, set this to ""

C_GREEN = (0, 1, 0.667, 1)
C_RED = (1, 0.23, 0.36, 1)
C_WAIT = (1, 0.77, 0, 1)
COL = {"GREEN": C_GREEN, "RED": C_RED, "WAIT": C_WAIT}
SUB = {"GREEN": "BULLISH SIGNAL", "RED": "BEARISH SIGNAL", "WAIT": "ANALYZING"}

# Global state. Mutated on the GUI thread only (workers talk through Feed.q).
S = dict(
    running=True, market="crypto", symbol="BTC/USDT", interval="1m",
    candles=[], wins=0, losses=0, locked=None, label="WAIT", strength=0,
    last_label=None, last_voice=0.0, e9=[], e21=[], status="CONNECTING",
)


def speak(text):
    if not tts:
        return
    try:
        tts.speak(message=text)
    except Exception:
        pass


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 100:
        return f"{p:.3f}"
    if p >= 1:
        return f"{p:.5f}"
    return f"{p:.6f}"


# ------------------------------------------------------------- indicators
def ema(v, n):
    if not v:
        return []
    k = 2.0 / (n + 1)
    out = [v[0]]
    for x in v[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    if len(closes) <= n:
        return 50.0
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def macd(closes, fast=12, slow=26, sig=9):
    line = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    return line, ema(line, sig)


def predict(cs):
    """Returns (label, strength 0-100, ema9 list, ema21 list)."""
    if len(cs) < 35:
        return "WAIT", 0, [], []
    closes = [c["c"] for c in cs]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    r = rsi(closes)
    line, sg = macd(closes)
    h, hp = line[-1] - sg[-1], line[-2] - sg[-2]
    s = 0.0
    # RSI (max +-2)
    if r < 30:
        s += 2
    elif r < 40:
        s += 1
    elif r > 70:
        s -= 2
    elif r > 60:
        s -= 1
    # EMA9/21 relation + fresh cross (max +-3)
    s += 2 if e9[-1] > e21[-1] else -2
    if e9[-2] <= e21[-2] and e9[-1] > e21[-1]:
        s += 1
    elif e9[-2] >= e21[-2] and e9[-1] < e21[-1]:
        s -= 1
    # MACD histogram + momentum (max +-2)
    s += 1.5 if h > 0 else -1.5
    s += 0.5 if h > hp else -0.5
    # Trend: price vs EMA21 and its slope (max +-1)
    if closes[-1] > e21[-1] and e21[-1] > e21[-4]:
        s += 1
    elif closes[-1] < e21[-1] and e21[-1] < e21[-4]:
        s -= 1
    # 3-candle pattern on closed candles (max +-2)
    ups = sum(1 for c in cs[-4:-1] if c["c"] > c["o"])
    s += {3: 2, 2: 0.5, 1: -0.5, 0: -2}[ups]
    strength = int(min(100, abs(s) / 10.0 * 100))
    label = "GREEN" if s >= 3 else "RED" if s <= -3 else "WAIT"
    return label, strength, e9, e21


# ------------------------------------------------------------------- feed
def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _parse_td(values):
    out = []
    for v in values:
        try:
            ts = calendar.timegm(time.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S"))
            out.append({"t": ts, "o": float(v["open"]), "h": float(v["high"]),
                        "l": float(v["low"]), "c": float(v["close"])})
        except Exception:
            continue
    return out


class Feed:
    """Network workers (daemon threads). They only append to a deque; the GUI drains it."""

    def __init__(self):
        self.q = deque(maxlen=2000)
        self.gen = 0

    def push(self, g, kind, val):
        self.q.append((g, kind, val))

    def alive(self, g):
        return S["running"] and g == self.gen

    def sleep(self, g, secs):
        end = time.time() + secs
        while time.time() < end:
            if not self.alive(g):
                return False
            time.sleep(0.1)
        return True

    def start(self, market, symbol, interval):
        self.gen += 1
        self.q.clear()
        target = self._crypto if market == "crypto" else self._forex
        threading.Thread(target=target, args=(self.gen, symbol, interval), daemon=True).start()

    # ---- crypto
    def _crypto(self, g, symbol, interval):
        sym = symbol.replace("/", "").lower()
        backoff = 1
        while self.alive(g):
            try:
                self.push(g, "status", "CONNECTING")
                r = requests.get("https://api.binance.com/api/v3/klines", timeout=8,
                                 params=dict(symbol=sym.upper(), interval=interval, limit=100))
                rows = r.json()
                self.push(g, "hist", [{"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
                                       "l": float(k[3]), "c": float(k[4])} for k in rows])
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._ws(g, sym, interval))
                finally:
                    loop.close()
                backoff = 1
            except Exception:
                self.push(g, "status", "RECONNECTING")
            if not self.sleep(g, backoff):
                return
            backoff = min(backoff * 2, 15)

    async def _ws(self, g, sym, interval):
        import websockets
        url = f"wss://stream.binance.com:9443/stream?streams={sym}@kline_{interval}"
        kw = dict(ping_interval=20, ping_timeout=20)
        ctx = _ssl_ctx()
        if ctx:
            kw["ssl"] = ctx
        async with websockets.connect(url, **kw) as ws:
            self.push(g, "status", "LIVE")
            while self.alive(g):
                try:
                    raw = await asyncio.wait_for(ws.recv(), 1.0)
                except asyncio.TimeoutError:
                    continue
                k = json.loads(raw)["data"]["k"]
                self.push(g, "tick", {"t": int(k["t"]) // 1000, "o": float(k["o"]), "h": float(k["h"]),
                                      "l": float(k["l"]), "c": float(k["c"])})

    # ---- forex
    def _forex(self, g, symbol, interval):
        iv = TD_IV[interval]
        cache, first = {}, True
        self.push(g, "status", "CONNECTING")
        while self.alive(g):
            wait = 5
            hit = cache.get((symbol, iv))
            if not hit or time.time() - hit[0] >= 15:  # 15 s cache, 5 s poll
                try:
                    r = requests.get(TD_URL, timeout=8, params=dict(
                        symbol=symbol, interval=iv, outputsize=100, order="ASC",
                        timezone="UTC", apikey=API_KEY))
                    j = r.json()
                    if j.get("status") == "error" or "values" not in j:
                        self.push(g, "status", "API: " + str(j.get("message", "error"))[:42])
                        wait = 15
                    else:
                        cs = _parse_td(j["values"])
                        cache[(symbol, iv)] = (time.time(), cs)
                        if first:
                            self.push(g, "hist", cs)
                            first = False
                        else:
                            for c in cs[-2:]:
                                self.push(g, "tick", c)
                        self.push(g, "status", "LIVE")
                except Exception:
                    self.push(g, "status", "NETWORK ERROR")
                    wait = 15
            if not self.sleep(g, wait):
                return


# ---------------------------------------------------------------- widgets
def L(text="", size=14, color=(1, 1, 1, 1), bold=False, **kw):
    lb = Label(text=text, font_size=sp(size), color=color, bold=bold,
               halign="center", valign="middle", **kw)
    lb.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lb


class GradientBG(Widget):
    """Fallback background: dark gradient bands with animated alpha."""
    N = 14

    def __init__(self, **kw):
        super().__init__(**kw)
        self.t = 0.0
        with self.canvas:
            Color(0.02, 0.04, 0.08, 1)
            self.base = Rectangle()
            self.bands = []
            for _ in range(self.N):
                c = Color(0, 0.4, 0.35, 0.05)
                self.bands.append((c, Rectangle()))
        self.bind(pos=self._lay, size=self._lay)
        self._ev = Clock.schedule_interval(self._anim, 1 / 12)
        self._lay()

    def _lay(self, *a):
        self.base.pos, self.base.size = self.pos, self.size
        bh = self.height / self.N
        for i, (_, r) in enumerate(self.bands):
            r.pos = (self.x, self.y + i * bh)
            r.size = (self.width, bh + 1)

    def _anim(self, dt):
        self.t += dt
        for i, (c, _) in enumerate(self.bands):
            k = (math.sin(self.t * 0.8 + i * 0.5) + 1) / 2
            c.rgba = (0, 0.25 + 0.25 * k, 0.45 - 0.2 * k, 0.06 + 0.16 * k)

    def stop(self):
        self._ev.cancel()


def make_bg():
    try:
        from kivy.resources import resource_find
        path = resource_find("bg.mp4")
        if not path:
            raise FileNotFoundError("bg.mp4")
        from kivy.core.video import Video as CoreVideo
        if CoreVideo is None:
            raise RuntimeError("no video provider (ffpyplayer missing)")
        from kivy.uix.video import Video
        try:
            v = Video(source=path, state="play", options={"eos": "loop"}, opacity=0.4,
                      volume=0, fit_mode="cover")
        except TypeError:  # very old Kivy
            v = Video(source=path, state="play", options={"eos": "loop"}, opacity=0.4,
                      volume=0, allow_stretch=True, keep_ratio=False)
        return v
    except Exception:
        return GradientBG()


class Chart(Widget):
    """Candlestick chart drawn with raw kivy.graphics (last 30 candles + EMA9/21)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.dirty = True
        self.bind(pos=self._d, size=self._d)

    def _d(self, *a):
        self.dirty = True

    def redraw(self, cs, e9, e21):
        self.dirty = False
        self.canvas.clear()
        x, y, w, h = self.x, self.y, self.width, self.height
        with self.canvas:
            Color(0.05, 0.07, 0.1, 0.55)
            Rectangle(pos=self.pos, size=self.size)
            n = min(CHART_N, len(cs))
            if n < 2:
                return
            view = cs[-n:]
            hi = max(c["h"] for c in view)
            lo = min(c["l"] for c in view)
            if hi <= lo:
                hi = lo + 1e-9
            pad = h * 0.06
            ph = h - 2 * pad

            def Y(v):
                return y + pad + (v - lo) / (hi - lo) * ph

            slot = w / CHART_N
            bw = max(1.0, slot * 0.6)
            off = CHART_N - n
            xs = [x + slot * (i + off + 0.5) for i in range(n)]
            for cx, c in zip(xs, view):
                Color(*(C_GREEN if c["c"] >= c["o"] else C_RED))
                Line(points=[cx, Y(c["l"]), cx, Y(c["h"])], width=1)
                top, bot = Y(max(c["o"], c["c"])), Y(min(c["o"], c["c"]))
                Rectangle(pos=(cx - bw / 2, bot), size=(bw, max(1.0, top - bot)))
            for series, col in ((e9, (1, 0.9, 0, 1)), (e21, (0.25, 0.55, 1, 1))):
                if len(series) >= n:
                    pts = []
                    for cx, v in zip(xs, series[-n:]):
                        pts += [cx, Y(v)]
                    Color(*col)
                    Line(points=pts, width=1.2)


class PredBox(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", padding=dp(6), **kw)
        with self.canvas.before:
            Color(0, 0, 0, 0.45)
            self._bg = Rectangle()
            self._c = Color(1, 1, 1, 1)
            self._ln = Line(rectangle=(0, 0, 1, 1), width=dp(2.5))
        self.bind(pos=self._upd, size=self._upd)
        self.main = L("WAIT", 50, C_WAIT, True, size_hint_y=0.58)
        self.stren = L("Strength: 0", 15, size_hint_y=0.21)
        self.sub = L("ANALYZING", 13, (1, 1, 1, 0.8), size_hint_y=0.21)
        for wdg in (self.main, self.stren, self.sub):
            self.add_widget(wdg)

    def _upd(self, *a):
        self._bg.pos, self._bg.size = self.pos, self.size
        self._ln.rectangle = (self.x, self.y, self.width, self.height)

    def set(self, label, strength):
        col = COL[label]
        self._c.rgba = col
        self.main.text, self.main.color = label, col
        self.stren.text = f"Strength: {strength}"
        self.sub.text = SUB[label]


# -------------------------------------------------------------------- app
class BotApp(App):
    title = "Candle Predictor"

    def build(self):
        Window.clearcolor = (0.02, 0.03, 0.06, 1)
        if platform not in ("android", "ios"):
            Window.size = (405, 800)
        self.popup = None
        self.bgm = None
        self.feed = Feed()
        self._last_calc = 0.0
        self._last_ui = 0.0
        self._calc_needed = True

        root = FloatLayout()
        self.bg = make_bg()
        root.add_widget(self.bg)  # first child = drawn behind everything

        col = BoxLayout(orientation="vertical", spacing=dp(4),
                        padding=[dp(8), dp(24), dp(8), dp(6)])
        col.add_widget(L(HEADER_ICON + "NEXT CANDLE PREDICTION", 17, C_GREEN, True, size_hint_y=0.06))
        self.l_info = L("", 13, markup=True, size_hint_y=0.05)
        col.add_widget(self.l_info)
        self.chart = Chart(size_hint_y=0.40)
        col.add_widget(self.chart)
        self.pbox = PredBox(size_hint_y=0.21)
        col.add_widget(self.pbox)
        self.l_stats = L("", 13, markup=True, size_hint_y=0.05)
        col.add_widget(self.l_stats)
        row = BoxLayout(size_hint_y=0.05)
        self.l_status = L("", 12, markup=True)
        self.l_timer = L("", 12)
        row.add_widget(self.l_status)
        row.add_widget(self.l_timer)
        col.add_widget(row)
        btns = BoxLayout(size_hint_y=0.08, spacing=dp(6))
        for txt, cb in (("[M] Market", self.open_market), ("[T] Timeframe", self.open_tf),
                        ("[R] Reset", lambda *a: self.reset()), ("[Q] Quit", lambda *a: self.stop())):
            b = Button(text=txt, font_size=sp(12), background_normal="",
                       background_color=(0.08, 0.2, 0.2, 0.9))
            b.bind(on_release=cb)
            btns.add_widget(b)
        col.add_widget(btns)
        col.add_widget(L("BOT BY NIROB | Signals are indicators, not financial advice", 9,
                         (1, 1, 1, 0.6), size_hint_y=0.05))
        root.add_widget(col)

        Window.bind(on_key_down=self._on_key)
        Clock.schedule_interval(self._tick, 1 / 30)
        Clock.schedule_once(self._check_video, 5)
        Clock.schedule_once(lambda dt: speak("Signal Bot Started"), 2.0)
        self._start_bgm()
        self.reset()
        return root

    # ---- media
    def _start_bgm(self):
        try:
            s = SoundLoader.load("bgm.mp3")
            if s:
                s.loop = True
                s.volume = 0.3
                s.play()
                self.bgm = s
        except Exception:
            self.bgm = None

    def _check_video(self, dt):
        v = self.bg
        if getattr(v, "texture", True) is None:  # video widget exists but never decoded a frame
            try:
                v.state = "stop"
                v.unload()
            except Exception:
                pass
            self.root.remove_widget(v)
            self.bg = GradientBG()
            self.root.add_widget(self.bg, index=len(self.root.children))

    def on_pause(self):
        try:
            if self.bgm:
                self.bgm.stop()
            if hasattr(self.bg, "state"):
                self.bg.state = "pause"
        except Exception:
            pass
        return True

    def on_resume(self):
        try:
            if self.bgm:
                self.bgm.play()
            if hasattr(self.bg, "state"):
                self.bg.state = "play"
        except Exception:
            pass

    def on_stop(self):
        S["running"] = False
        self.feed.gen += 1
        try:
            if self.bgm:
                self.bgm.stop()
        except Exception:
            pass

    # ---- state
    def reset(self):
        S.update(candles=[], wins=0, losses=0, locked=None, label="WAIT", strength=0,
                 last_label=None, e9=[], e21=[], status="CONNECTING")
        self.chart.dirty = True
        self._ui()
        self._ui_stats()
        self.feed.start(S["market"], S["symbol"], S["interval"])

    def _drain(self):
        q = self.feed.q
        while True:
            try:
                g, kind, val = q.popleft()
            except IndexError:
                break
            if g != self.feed.gen:
                continue
            if kind == "status":
                S["status"] = val
            elif kind == "hist":
                S["candles"] = val[-MAX_CANDLES:]
                S["locked"] = None
                self._calc_needed = True
                self.chart.dirty = True
            elif kind == "tick":
                self._apply_tick(val)

    def _apply_tick(self, c):
        cs = S["candles"]
        if not cs:
            cs.append(c)
        else:
            last = cs[-1]
            if c["t"] == last["t"]:
                last.update(c)
            elif c["t"] > last["t"]:
                # previous candle just closed -> score the prediction made when it opened
                if len(cs) > 1 and S["locked"] in ("GREEN", "RED"):
                    actual = "GREEN" if last["c"] > cs[-2]["c"] else "RED"
                    if actual == S["locked"]:
                        S["wins"] += 1
                    else:
                        S["losses"] += 1
                    self._ui_stats()
                cs.append(c)
                if len(cs) > MAX_CANDLES:
                    del cs[0]
                S["locked"] = predict(cs)[0]
            else:
                return
        self._calc_needed = True
        self.chart.dirty = True

    def _recalc(self):
        S["label"], S["strength"], S["e9"], S["e21"] = predict(S["candles"])
        self._ui()

    # ---- UI refresh
    def _tick(self, dt):
        self._drain()
        now = time.time()
        if self._calc_needed and now - self._last_calc >= 0.2:
            self._last_calc = now
            self._calc_needed = False
            self._recalc()
        if self.chart.dirty:
            self.chart.redraw(S["candles"], S["e9"], S["e21"])
        if now - self._last_ui >= 0.5:
            self._last_ui = now
            self._ui_timer()

    def _ui(self):
        cs = S["candles"]
        if cs:
            p = cs[-1]["c"]
            prev = cs[-2]["c"] if len(cs) > 1 else p
            pct = (p - prev) / prev * 100 if prev else 0.0
            hexc = "00ffaa" if pct >= 0 else "ff3b5c"
            self.l_info.text = (f"{S['symbol']} | {S['interval']} | {fmt_price(p)} | "
                                f"[color={hexc}]{pct:+.2f}%[/color]")
        else:
            self.l_info.text = f"{S['symbol']} | {S['interval']} | --"
        self.pbox.set(S["label"], S["strength"])
        lab = S["label"]
        if lab != S["last_label"]:  # speak only on change (+5 s debounce)
            S["last_label"] = lab
            if lab in ("GREEN", "RED") and time.time() - S["last_voice"] > 5:
                S["last_voice"] = time.time()
                speak("Buy Signal, Green Candle" if lab == "GREEN" else "Sell Signal, Red Candle")

    def _ui_stats(self):
        w, l = S["wins"], S["losses"]
        tot = w + l
        if tot:
            acc = w / tot * 100
            hexc = "00ffaa" if acc >= 60 else "ffc400" if acc >= 50 else "ff3b5c"
            a = f"[color={hexc}]{acc:.0f}%[/color]"
        else:
            a = "--"
        self.l_stats.text = (f"Wins [color=00ffaa]{w}[/color]   Losses [color=ff3b5c]{l}[/color]   "
                             f"Accuracy {a}")

    def _ui_timer(self):
        st = S["status"]
        if st == "LIVE":
            self.l_status.text = "[color=00ffaa]● LIVE[/color]"
        elif st in ("CONNECTING", "RECONNECTING"):
            self.l_status.text = f"[color=ffc400]● {st}[/color]"
        else:
            self.l_status.text = f"[color=ff3b5c]● {st}[/color]"
        sec = IV_SEC[S["interval"]]
        rem = sec - (int(time.time()) % sec)
        self.l_timer.text = f"Next candle: {rem // 60:02d}:{rem % 60:02d}"

    # ---- popups
    def _open_list(self, title, entries, current, on_pick):
        if self.popup:
            return
        lst = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        lst.bind(minimum_height=lst.setter("height"))
        for e in entries:
            if e.startswith("--"):
                lst.add_widget(L(e.strip("- "), 12, C_GREEN, True, size_hint_y=None, height=dp(28)))
                continue
            b = Button(text=e, size_hint_y=None, height=dp(48), background_normal="",
                       background_color=(0, 0.65, 0.45, 1) if e == current else (0.15, 0.17, 0.22, 1))
            b.bind(on_release=lambda w, v=e: on_pick(v))
            lst.add_widget(b)
        sv = ScrollView()
        sv.add_widget(lst)
        self.popup = Popup(title=title, content=sv, size_hint=(0.85, 0.75))
        self.popup.bind(on_dismiss=lambda *a: setattr(self, "popup", None))
        self.popup.open()

    def open_market(self, *a):
        self._open_list("Select Market", ["-- CRYPTO --"] + CRYPTO + ["-- FOREX --"] + FOREX,
                        S["symbol"], self._pick_market)

    def open_tf(self, *a):
        self._open_list("Select Timeframe", INTERVALS, S["interval"], self._pick_tf)

    def _pick_market(self, sym):
        S["symbol"] = sym
        S["market"] = "crypto" if sym in CRYPTO else "forex"
        self.popup.dismiss()
        self.reset()
        speak("Market " + sym.replace("/", " "))

    def _pick_tf(self, iv):
        S["interval"] = iv
        self.popup.dismiss()
        self.reset()
        speak("Timeframe " + IV_SPOKEN[iv])

    # ---- keyboard
    def _on_key(self, win, key, scancode, codepoint, modifier):
        if key == 27 and self.popup:  # Android back / Esc closes popup first
            self.popup.dismiss()
            return True
        ch = (codepoint or "").lower()
        if ch == "m":
            self.open_market()
        elif ch == "t":
            self.open_tf()
        elif ch == "r":
            self.reset()
        elif ch == "q":
            self.stop()
        else:
            return False
        return True


if __name__ == "__main__":
    BotApp().run()
