"""
Prueba sin red del marcador bayesiano (edge.py): etiquetas, encogimiento hacia el a priori, silenciador,
tamaño por ventaja, separación reales/sombra y persistencia en JSON.
  python test_edge.py
"""
import json
import random
import time

from edge import Scoreboard, adx_bucket, is_operational, labels_of, norm_reason, session_of

sig = {"kind": "LPS", "tf": "15m", "side": "LONG", "ctx_align": "a favor", "against_trend": False,
       "btc_align": "neutral", "adx": 22.0}
lb = labels_of(sig, 0)
assert lb["setup"] == "LPS|15m" and lb["sesion"] == "Asia 00-08" and lb["tendencia"] == "a favor/neutral", lb
assert session_of(9 * 3600) == "Europa 08-16" and session_of(20 * 3600) == "EEUU 16-24"
assert adx_bucket(None) == "ADX sin dato" and adx_bucket(10) == "ADX bajo" and adx_bucket(25) == "ADX medio" and adx_bucket(40) == "ADX alto"
print("1 OK etiquetas")

# 2) motivos: normalización y qué cuenta como operativo
assert norm_reason("bot en pausa (/reanudar)") == "bot en pausa" and is_operational("bot en pausa")
assert norm_reason("R:R 1.20 < 1.5") == "R:R bajo" and not is_operational("R:R bajo")
assert norm_reason("estructura de BTC 4h en contra") == "BTC en contra (bloqueo)"
assert norm_reason("máximo de la cuenta (1/1)") == "tope de posiciones de la cuenta"
assert is_operational(norm_reason("FAIL_TRADES=aviso (solo aviso, se registra para medir)"))
print("2 OK motivos")

# 3) encogimiento: pocas operaciones no mueven la media posterior; muchas sí
sb = Scoreboard({}, prior_n=20, prior_mean=0.0)
for _ in range(3):
    sb.add(lb, 2.0, "live")                      # 3 golpes de suerte
m3 = sb.summary("setup", "LPS|15m")
assert m3["mean"] == 2.0 and 0 < m3["post"] < 0.35, m3          # 6/(3+20) = 0.26
for _ in range(197):
    sb.add(lb, 2.0, "shadow")
m200 = sb.summary("setup", "LPS|15m")
assert m200["n"] == 200 and m200["post"] > 1.7, m200
assert sb.summary("setup", "LPS|15m", "live")["n"] == 3 and sb.summary("setup", "LPS|15m", "shadow")["n"] == 197
print(f"3 OK a priori: n=3 → posterior {m3['post']:+.2f}R · n=200 → {m200['post']:+.2f}R")

# 4) silenciador: exige n mínimo, media posterior claramente negativa y t negativo
rnd = random.Random(7)
sb = Scoreboard({})
bad = {**lb, "setup": "SOS|15m"}
for _ in range(10):
    sb.add(bad, -1.0 + rnd.uniform(-0.2, 0.2), "shadow")
assert sb.muted(bad, 20, 0.15, 1.5) is None                      # n < 20: no se silencia
for _ in range(40):
    sb.add(bad, -1.0 + rnd.uniform(-0.2, 0.2), "shadow")
msg = sb.muted(bad, 20, 0.15, 1.5)
assert msg and "setup=SOS|15m" in msg, msg
good = {**lb, "setup": "LPS|15m", "sesion": "EEUU 16-24"}
for _ in range(50):
    sb.add(good, 0.4 + rnd.uniform(-0.8, 0.8), "live")
assert sb.muted(good, 20, 0.15, 1.5) is None
# una vez silenciado sigue acumulando sombra: si mejora, se levanta solo
for _ in range(400):
    sb.add(bad, 1.5 + rnd.uniform(-0.2, 0.2), "shadow")
assert sb.muted(bad, 20, 0.15, 1.5) is None
print("4 OK silenciador (y se levanta solo si el setup se recupera)")

# 5) tamaño por ventaja: acotado
sb = Scoreboard({}, prior_n=10)
assert sb.size_mult(lb, 1.0, 0.5, 1.25) == 1.0                    # sin datos: tamaño normal
for _ in range(60):
    sb.add(lb, 1.0, "live")
assert sb.size_mult(lb, 1.0, 0.5, 1.25) == 1.25                    # tope superior
sb2 = Scoreboard({})
for _ in range(60):
    sb2.add(lb, -1.0, "live")
assert sb2.size_mult(lb, 1.0, 0.5, 1.25) == 0.5                    # tope inferior
print("5 OK tamaño acotado 0.5–1.25")

# 6) persistencia: sobrevive a un ciclo JSON (state.json) y los totales cuadran
data = {}
sb = Scoreboard(data)
sb.add(lb, 1.5, "live"); sb.add(lb, -1.0, "shadow"); sb.add({"motivo": "bot en pausa"}, -1.0, "shadow")
sb2 = Scoreboard(json.loads(json.dumps(data)))
assert sb2.totals("live") == (1, 1.5) and sb2.totals("shadow") == (1, -1.0), (sb2.totals("live"), sb2.totals("shadow"))
rows = sb2.shadow_by_reason()
assert rows and rows[0][0] == "bot en pausa" and rows[0][1] == 1
assert "setup=LPS|15m" in sb2.report(min_n=1)
print("6 OK persistencia y totales")
print("TODO OK")
