"""Marcador bayesiano de ventaja + etiquetas de señal (v4.5). Módulo puro: sin red, sin estado propio.

Problema que resuelve: casi todas las señales NO se operan (pausa, topes de posiciones, filtros en 'aviso',
pérdida diaria) y con pocas operaciones reales nunca hay muestra para saber qué funciona.
Solución: cada señal bloqueada por motivos OPERATIVOS se sigue como operación virtual ('libro sombra', ver main.py)
y su resultado entra aquí junto al de las operaciones reales. Así la muestra crece varias veces más rápido y se mide
cuánto cuesta cada bloqueo y cada etiqueta (contra tendencia, ADX alto, sesión, setup...).

Con muestras pequeñas no hay que fiarse de la media observada: se encoge hacia un a priori
    media_posterior = (suma_R + n0 · mu0) / (n + n0)          (n0 = EDGE_PRIOR_N, mu0 = EDGE_PRIOR_MEAN_R)

Aviso honesto: mirar muchos grupos a la vez produce falsos hallazgos por azar. Por eso solo 'setup' y 'sesion'
pueden silenciar señales y solo con n mínimo, media posterior claramente negativa y t negativo.
"""
import html
import math
from datetime import datetime, timezone

GROUPS = ("setup", "sesion", "side", "ctx", "tendencia", "btc", "adx", "motivo")
MUTE_GROUPS = ("setup", "sesion")

# motivos de no apertura que son OPERATIVOS (no dicen nada de la calidad de la señal): su resultado virtual
# entra en los grupos. Los demás (R:R bajo, stop mal, filtros en 'bloquea'...) solo alimentan el grupo 'motivo'.
OPERATIONAL = ("bot en pausa", "tope de posiciones del bot", "tope de posiciones de la cuenta", "pérdida diaria",
               "misma dirección", "trampa en aviso", "edge negativo")

_REASONS = (
    ("pausa", "bot en pausa"),
    ("máximo del bot", "tope de posiciones del bot"),
    ("máximo de la cuenta", "tope de posiciones de la cuenta"),
    ("pérdida diaria", "pérdida diaria"),
    ("MAX_SAME_SIDE", "misma dirección"),
    ("FAIL_TRADES", "trampa en aviso"),
    ("edge negativo", "edge negativo"),
    ("R:R", "R:R bajo"),
    ("stop a", "distancia del stop"),
    ("BTC", "BTC en contra (bloqueo)"),
    ("estructura", "contexto en contra (bloqueo)"),
    ("contra tendencia", "contra tendencia (bloqueo)"),
    ("ADX", "ADX alto (bloqueo)"),
    ("impulso", "impulso sin agotar (bloqueo)"),
    ("VWAP", "AVWAP en contra (bloqueo)"),
    ("amplitud", "amplitud en contra (bloqueo)"),
    ("meta-modelo", "meta-modelo (bloqueo)"),
)


def norm_reason(text):
    for key, label in _REASONS:
        if key in text:
            return label
    return text[:30]


def is_operational(reason_label):
    return reason_label in OPERATIONAL


def session_of(ts):
    h = datetime.fromtimestamp(ts, timezone.utc).hour
    return "Asia 00-08" if h < 8 else "Europa 08-16" if h < 16 else "EEUU 16-24"


def adx_bucket(x):
    if x is None or x != x:
        return "ADX sin dato"
    return "ADX bajo" if x < 20 else "ADX medio" if x < 30 else "ADX alto"


def labels_of(sig, ts):
    """Etiquetas de una señal; mismas dimensiones para operaciones reales y virtuales."""
    return {
        "setup": f"{sig.get('kind', '-')}|{sig.get('tf', '-')}",
        "sesion": session_of(ts),
        "side": sig.get("side", "-"),
        "ctx": sig.get("ctx_align") or "-",
        "tendencia": "contra EMA" if sig.get("against_trend") else "a favor/neutral",
        "btc": sig.get("btc_align") or "-",
        "adx": adx_bucket(sig.get("adx")),
    }


class Scoreboard:
    """Envoltorio sobre un dict (el que se guarda en state.json): clave 'grupo|valor' → {'live': [...], 'shadow': [...]}
    con [n, suma_R, suma_R², ganadoras]."""

    def __init__(self, data, prior_n=20.0, prior_mean=0.0):
        self.d = data
        self.n0, self.mu0 = float(prior_n), float(prior_mean)

    def add(self, labels, r, src):
        for g, v in labels.items():
            rec = self.d.setdefault(f"{g}|{v}", {"live": [0, 0.0, 0.0, 0], "shadow": [0, 0.0, 0.0, 0]})
            a = rec[src]
            a[0] += 1
            a[1] += r
            a[2] += r * r
            a[3] += 1 if r > 0 else 0

    def _raw(self, g, v, src=None):
        rec = self.d.get(f"{g}|{v}")
        if not rec:
            return 0, 0.0, 0.0, 0
        parts = [rec[src]] if src else [rec["live"], rec["shadow"]]
        return tuple(sum(p[i] for p in parts) for i in range(4))

    def summary(self, g, v, src=None):
        n, s, s2, w = self._raw(g, v, src)
        mean = s / n if n else 0.0
        sd = math.sqrt(max(0.0, (s2 - s * s / n) / (n - 1))) if n > 1 else 0.0
        t = mean / (sd / math.sqrt(n)) if n > 2 and sd > 0 else 0.0
        post = (s + self.n0 * self.mu0) / (n + self.n0)
        return {"n": n, "sum": s, "mean": mean, "sd": sd, "t": t, "post": post, "wins": w,
                "wr": w / n if n else 0.0}

    def muted(self, labels, min_n, mute_r, t_thr):
        """Motivo (texto) si algún grupo silenciable tiene ventaja claramente negativa; si no, None."""
        for g in MUTE_GROUPS:
            v = labels.get(g)
            if v is None:
                continue
            m = self.summary(g, v)
            if m["n"] >= min_n and m["post"] < -mute_r and m["t"] <= -t_thr:
                return f"edge negativo en {g}={v}: media {m['mean']:+.2f}R en {m['n']} ops (reales+sombra)"
        return None

    def size_mult(self, labels, k, lo, hi):
        """Multiplicador de riesgo según la media posterior del setup (encogida hacia el a priori)."""
        v = labels.get("setup")
        if v is None:
            return 1.0
        return max(lo, min(hi, 1.0 + k * self.summary("setup", v)["post"]))

    def totals(self, src):
        """(n, suma_R) de una fuente ('live' | 'shadow'); cada señal tiene exactamente un valor de 'side'."""
        n, tot = 0, 0.0
        for key in self.d:
            if key.startswith("side|"):
                k, s_, _, _ = self._raw("side", key.split("|", 1)[1], src)
                n += k
                tot += s_
        return n, tot

    def report(self, min_n=5, top=14):
        rows = []
        for key in self.d:
            g, v = key.split("|", 1)
            if g == "motivo":
                continue
            m = self.summary(g, v)
            if m["n"] >= min_n:
                live = self._raw(g, v, "live")[0]
                rows.append((m["post"], g, v, m, live))
        if not rows:
            return "🧪 Marcador: aún no hay grupos con %d o más operaciones (reales + sombra)." % min_n
        rows.sort(key=lambda x: x[0])
        half = top // 2
        pick = rows if len(rows) <= top else rows[:half] + rows[-(top - half):]
        lines = ["🧪 <b>Marcador de ventaja</b> (reales + sombra · media encogida hacia %+.2fR con peso %g)" %
                 (self.mu0, self.n0)]
        for post, g, v, m, live in pick:
            lines.append(f"• {g}={html.escape(v)}: {m['n']} ops ({live} reales) · media {m['mean']:+.2f}R · "
                         f"posterior {post:+.2f}R · acierto {m['wr'] * 100:.0f}%")
        lines.append("Orden: de peor a mejor. Con pocas operaciones es ruido; mira n antes de fiarte.")
        return "\n".join(lines)

    def shadow_by_reason(self, min_n=1):
        rows = []
        for key in self.d:
            g, v = key.split("|", 1)
            if g != "motivo":
                continue
            m = self.summary(g, v, "shadow")
            if m["n"] >= min_n:
                rows.append((m["sum"], v, m))
        rows.sort(reverse=True)
        return [(v, m["n"], m["sum"], m["mean"], m["wr"]) for _, v, m in rows]
