"""
¿Sirve de algo la brecha entre reputación y forma?

LA IDEA

    brecha = diferencia de Elo − diferencia de forma

Cuando sale grande y positiva significa "el historial dice que el local es
muy superior, pero los resultados recientes no lo dicen". Villarreal-Depor
en la jornada 4 de 2026-27 es el caso de manual: el Villarreal con 2 puntos
de 9 y el Depor invicto, y el modelo dando un 61% al local.

POR QUÉ PUEDE APORTAR ALGO

La regresión logística es lineal: tiene el Elo y tiene la forma, y las suma.
Puede decir "más Elo, más probable" y "peor forma, menos probable". Lo que no
puede decir de ninguna manera es "un equipo fuerte en mala forma es
DESPROPORCIONADAMENTE vulnerable", porque eso es un producto y una suma nunca
produce un producto.

O sea que no es información que estemos contando mal: es información que el
modelo, tal como está construido, no puede representar.

DOS PREGUNTAS DISTINTAS

    A. ¿Predice mejor el RESULTADO?
       Objeción seria: el mercado ya sabe que el Villarreal lleva 2 puntos,
       y eso ya está en su precio. Si usamos el mercado como entrada,
       añadir esto puede ser contar lo mismo dos veces.

    B. ¿Predice el ERROR DEL PÚBLICO?
       Aquí la objeción no aplica. No decimos que el mercado se equivoque:
       decimos que el público va por detrás actualizando reputaciones. Y
       como el valor es mercado ÷ público, predecir el error del público es
       predecir el valor, que es lo que se cobra.

RESULTADO DE A, YA MEDIDO: no aporta, y no por falta de muestra. La
interacción se activa en 962 de 4.407 partidos y aun así empeora el
log-loss (−0,0005 el producto, −0,0002 la asimétrica). El control —la resta
elo − forma, que es colineal y no puede aportar nada— dio +0,0001, así que
el test estaba bien montado. La pregunta A está cerrada en negativo.

CÓMO SE MIDE B SIN CUOTAS

La primera versión comparaba al público con el mercado, y eso dejó fuera
todo el histórico: la captura de cuotas empezó con este proyecto, así que
solo existen las de las últimas jornadas. Los porcentajes de LAE, en
cambio, sí están desde el principio.

Así que se compara al público con lo que de verdad pasó, que además es una
pregunta mejor: si la gente juega un signo al 60%, ese signo debería salir
el 60% de las veces. Cuando no sale, ahí está el valor. La pregunta es si
falla más donde la reputación va por delante de la forma.

Se parte por cuartiles de brecha, no por un umbral elegido a mano —un
umbral se puede mover hasta que salga lo que uno quiere— y se comprueba con
una prueba de permutación: se baraja la brecha 2.000 veces y se mira cuántas
veces el azar produce una diferencia igual de grande.

ESTE SCRIPT NO CAMBIA NADA. Solo mide y escribe. Si las correlaciones no
están, la idea se cae y no hemos tocado el modelo.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.api_client import ApiIngesta          # noqa: E402
from modelado import caracteristicas                  # noqa: E402


def descargar_resultados(api: ApiIngesta) -> list[dict]:
    """Todos los partidos con resultado, paginando."""
    filas: list[dict] = []
    offset = 0
    while True:
        d = api.contexto_resultados(limite=1000, offset=offset)
        lote = d.get("items", [])
        filas.extend(lote)
        if not d.get("hay_mas") or not lote:
            break
        offset += len(lote)
    return filas


def clave(fecha: str, local: str, visitante: str) -> str:
    """
    Identificador de un partido que aguanta pequeñas diferencias de nombre.

    Los resultados y el boleto de la quiniela no siempre escriben igual a los
    equipos ('Ath.Club' contra 'Athletic Club'), así que se compara por los
    primeros caracteres del nombre normalizado más el día. No es perfecto,
    pero el script informa de cuántos ha podido emparejar: si el número sale
    bajo, se ve enseguida en lugar de dar un resultado silenciosamente malo.
    """
    def limpio(s: str) -> str:
        s = (s or "").lower()
        for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"),
                     ("ú", "u"), ("ñ", "n"), (".", ""), (" ", ""), ("-", "")):
            s = s.replace(a, b)
        return s[:6]

    return f"{(fecha or '')[:10]}|{limpio(local)}|{limpio(visitante)}"


def brechas(filas: list[dict]) -> tuple[dict[str, float], dict[str, str]]:
    """
    La brecha de cada partido, calculada con lo que se sabía ANTES de jugarlo.

    Se recorre en orden cronológico llamando a caracteristicas() antes de
    registrar(), igual que hace el entrenamiento. Hacerlo al revés daría una
    brecha calculada con el resultado ya dentro, que es la manera clásica de
    obtener una correlación preciosa y falsa.
    """
    partidos = []
    for f in filas:
        try:
            partidos.append((
                datetime.fromisoformat(f["fecha_hora_inicio"]),
                f["equipo_local"], f["equipo_visitante"],
                int(f["goles_local"]), int(f["goles_visitante"]),
                f.get("signo"), f.get("competicion", ""),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    partidos.sort()

    estado = caracteristicas.Estado()
    salida: dict[str, float] = {}
    signos: dict[str, str] = {}
    i_elo = caracteristicas.NOMBRES.index("elo_diferencia")
    i_forma = caracteristicas.NOMBRES.index("forma_diferencia")

    for dt, loc, vis, gl, gv, signo, comp in partidos:
        v = estado.caracteristicas(loc, vis, dt)
        # El Elo va dividido por 100 y la forma es una fracción de 0 a 1, así
        # que se ponen en la misma escala antes de restar. Sin esto la brecha
        # sería el Elo con un pellizco de forma.
        k = clave(dt.isoformat(), loc, vis)
        salida[k] = float(v[i_elo]) - float(v[i_forma])
        if signo:
            signos[k] = signo
        estado.registrar(loc, vis, dt, gl, gv)

    return salida, signos


def terna(d: dict | None, a: str, b: str, c: str) -> list[float] | None:
    if not d:
        return None
    try:
        v = [float(d[a]), float(d[b]), float(d[c])]
    except (KeyError, TypeError, ValueError):
        return None
    t = sum(v)
    return [x / t for x in v] if t > 0 else None


def correlacion(x: list[float], y: list[float]) -> tuple[float, float]:
    """
    Pearson, y el intervalo de confianza al 90% por bootstrap.

    Se da el intervalo y no solo el coeficiente porque con muestras pequeñas
    una correlación de 0,20 puede ser perfectamente compatible con cero, y
    entonces no dice nada. El número solo, sin su incertidumbre, invita a
    creerse lo que no toca.
    """
    xa, ya = np.array(x), np.array(y)
    if len(xa) < 20 or xa.std() == 0 or ya.std() == 0:
        return (float("nan"), float("nan"))
    r = float(np.corrcoef(xa, ya)[0, 1])

    rng = np.random.default_rng(11)
    muestras = []
    for _ in range(2000):
        i = rng.integers(0, len(xa), len(xa))
        if xa[i].std() == 0 or ya[i].std() == 0:
            continue
        muestras.append(np.corrcoef(xa[i], ya[i])[0, 1])
    if not muestras:
        return (r, float("nan"))
    lo, hi = np.percentile(muestras, [5, 95])
    return (r, float(hi - lo) / 2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--jornadas", type=int, default=50,
                   help="cuántas jornadas mirar hacia atrás")
    args = p.parse_args()

    api = ApiIngesta()

    print("Descargando resultados...")
    filas = descargar_resultados(api)
    print(f"  {len(filas)} partidos con resultado")

    print("Calculando la brecha de cada partido...")
    b, signos = brechas(filas)
    print(f"  {len(b)} brechas, {len(signos)} con signo conocido")

    # ------------------------------------------------------------------
    # Inventario: ¿hasta dónde llegan los porcentajes de LAE?
    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("INVENTARIO DE PORCENTAJES DE LAE")
    print("=" * 66)

    # contexto_quiniela topa en 50 por llamada, así que se recorre temporada
    # a temporada. Las temporadas se sacan de las fechas de los resultados:
    # no hay que mantener una lista a mano que se quede vieja cada agosto.
    años = sorted({
        (datetime.fromisoformat(f["fecha_hora_inicio"]).year
         if datetime.fromisoformat(f["fecha_hora_inicio"]).month >= 7
         else datetime.fromisoformat(f["fecha_hora_inicio"]).year - 1)
        for f in filas if f.get("fecha_hora_inicio")
    })
    temporadas = [f"{a}-{str(a + 1)[2:]}" for a in años]
    print(f"  temporadas con resultados: {', '.join(temporadas)}")

    jornadas = []
    for t in temporadas:
        try:
            lote = api.contexto_quiniela(temporada=t, limite=50)
        except Exception as exc:                     # noqa: BLE001
            print(f"    {t}: {exc}")
            continue
        jornadas.extend(lote)
    print(f"  {len(jornadas)} jornadas en la base")
    if args.jornadas and len(jornadas) > args.jornadas:
        jornadas = jornadas[-args.jornadas:]
        print(f"  se miran las {len(jornadas)} más recientes")

    muestras = []          # (brecha, desvio_publico, nombre)
    resultados = []        # (brecha, gano_el_favorito)
    con_lae = 0
    sin_emparejar = 0

    for j in jornadas:
        try:
            d = api.contexto_dashboard(
                temporada=j.get("etiqueta_temporada"),
                numero_jornada=int(j.get("numero_jornada")),
            )
        except Exception as exc:                     # noqa: BLE001
            print(f"    jornada {j.get('numero_jornada')}: {exc}")
            continue

        # Si algo vuelve a no cuadrar, mejor ver la forma real del dato que
        # deducirla de un cero.
        if not muestras and not resultados and d.get("casillas"):
            ej = next((x for x in d["casillas"]
                       if int(x.get("posicion", 0)) != 15), None)
            if ej is not None and not globals().get("_mostrado"):
                globals()["_mostrado"] = True
                print(f"    (ejemplo de casilla: probabilidades="
                      f"{list((ej.get('probabilidades') or {}).keys())} "
                      f"mercado={list((ej.get('mercado') or {}).keys())})")

        hubo = False
        for c in d.get("casillas", []):
            if int(c.get("posicion", 0)) == 15:
                continue
            pr = c.get("probabilidades") or {}
            lae = terna(pr.get("LAE_CIERRE") or pr.get("LAE_ESTIMADO"),
                        "p1", "px", "p2")
            # contexto_dashboard.php devuelve el mercado con p1/px/p2, no
            # con los nombres de la columna de la tabla. Usar los de la
            # tabla hacía que la terna fuese None siempre y el script
            # descartara todas las casillas antes de mirarlas.
            mer = terna(c.get("mercado"), "p1", "px", "p2")
            if not lae:
                continue
            hubo = True

            k = clave(c.get("fecha_hora_inicio") or "",
                      c.get("local") or "", c.get("visitante") or "")
            if k not in b:
                sin_emparejar += 1
                continue

            # El favorito del PÚBLICO: a quién juega más gente. La pregunta
            # es si ese favorito gana menos de lo que la gente cree cuando
            # arrastra reputación que su forma no respalda.
            i = int(np.argmax(lae))
            if i == 1:
                continue          # el empate no tiene "reputación" que medir

            # La brecha está orientada al local; para el visitante hay que
            # darle la vuelta, o los dos casos se cancelarían entre sí.
            brecha = b[k] if i == 0 else -b[k]
            real = signos.get(k)
            if real is None:
                continue

            acerto = 1.0 if real == ("1" if i == 0 else "2") else 0.0
            nombre = f"{c.get('local')} - {c.get('visitante')}"
            resultados.append((brecha, float(lae[i]), acerto, nombre))

            # El desvío contra el mercado solo cuando hay cuotas: en el
            # histórico no las hay, y esperar a tenerlas habría dejado sin
            # medir lo que ya se puede medir.
            if mer:
                muestras.append((brecha, lae[i] - mer[i], nombre))

        if hubo:
            con_lae += 1

    print(f"  {con_lae} jornadas con porcentajes de LAE")
    print(f"  {len(resultados)} casillas con LAE y resultado conocido")
    print(f"  {len(muestras)} de ellas con cuotas de mercado además")
    if sin_emparejar:
        print(f"  {sin_emparejar} casillas que no casan con ningún resultado")
        pct = 100 * sin_emparejar / max(sin_emparejar + len(resultados), 1)
        if pct > 20:
            print(f"    ATENCIÓN: es el {pct:.0f}%. Los nombres del boleto y")
            print("    los de la tabla de resultados no se están emparejando")
            print("    bien, y lo de abajo mide una submuestra sesgada.")

    # ------------------------------------------------------------------
    # B. ¿Acierta menos el público cuando juega a la reputación?
    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("B · ¿FALLA MÁS EL PÚBLICO CUANDO JUEGA A LA REPUTACIÓN?")
    print("=" * 66)
    print()
    print("  Para cada casilla se toma el signo que más juega la gente y se")
    print("  compara lo que la gente le da con lo que de verdad ocurrió.")
    print("  Si el público estuviera bien calibrado, un signo jugado al 60%")
    print("  saldría el 60% de las veces.")

    if len(resultados) < 100:
        print(f"\n  Solo hay {len(resultados)} casillas: insuficiente.")
    else:
        br = np.array([r[0] for r in resultados])
        pub = np.array([r[1] for r in resultados])
        ok = np.array([r[2] for r in resultados])

        # Se parte por cuartiles de brecha en vez de por un umbral elegido a
        # dedo: un umbral se puede mover hasta que salga lo que uno quiere.
        cortes = np.percentile(br, [25, 50, 75])
        tramos = [
            ("brecha baja (reputación ≈ forma)", br <= cortes[0]),
            ("brecha media-baja", (br > cortes[0]) & (br <= cortes[1])),
            ("brecha media-alta", (br > cortes[1]) & (br <= cortes[2])),
            ("brecha alta (reputación >> forma)", br > cortes[2]),
        ]

        print()
        print(f"  {'tramo':<36} {'n':>5} {'juega':>7} {'ocurre':>7} {'error':>8}")
        for nombre, m in tramos:
            if m.sum() < 20:
                continue
            juega = float(pub[m].mean())
            ocurre = float(ok[m].mean())
            print(f"  {nombre:<36} {int(m.sum()):>5} {juega:>6.1%} "
                  f"{ocurre:>6.1%} {ocurre - juega:>+8.1%}")

        print()
        print("  La columna 'error' es lo que importa. Negativa significa que")
        print("  el público se pasa: juega ese signo más de lo que ocurre.")
        print("  Lo que buscamos es que sea MÁS negativa en la brecha alta")
        print("  que en la baja. Si es igual de negativa en todos los tramos,")
        print("  el público se pasa siempre y la reputación no explica nada.")

        alto = tramos[3][1]
        bajo = tramos[0][1]
        if alto.sum() >= 20 and bajo.sum() >= 20:
            e_alto = float(ok[alto].mean() - pub[alto].mean())
            e_bajo = float(ok[bajo].mean() - pub[bajo].mean())
            print(f"\n  diferencia entre extremos: {e_alto - e_bajo:+.1%}")

            # ¿Es eso más de lo que daría el azar? Se baraja la brecha y se
            # mira cuántas veces sale una diferencia igual de grande por
            # casualidad. Sin esto, cualquier número parece un hallazgo.
            rng = np.random.default_rng(23)
            observado = e_alto - e_bajo
            veces = 0
            for _ in range(2000):
                mezcla = rng.permutation(br)
                ca = np.percentile(mezcla, [25, 75])
                a = mezcla > ca[1]
                z = mezcla <= ca[0]
                if a.sum() < 20 or z.sum() < 20:
                    continue
                d = ((ok[a].mean() - pub[a].mean())
                     - (ok[z].mean() - pub[z].mean()))
                if abs(d) >= abs(observado):
                    veces += 1
            p_valor = veces / 2000
            print(f"  probabilidad de ver esto por azar: {p_valor:.3f}")
            print()
            if p_valor < 0.05 and observado < 0:
                print("  HAY SEÑAL. El público sobrejuega al favorito con")
                print("  reputación por encima de su forma más de lo que")
                print("  sobrejuega a los demás. Eso es valor aprovechable.")
            elif p_valor < 0.05:
                print("  Hay señal, pero en la dirección CONTRARIA a la")
                print("  esperada. Merece mirarse antes de usarla.")
            else:
                print("  No hay señal: lo observado entra dentro de lo que")
                print("  produce el azar. La idea se descarta.")

        # Y la versión contra el mercado, si hay cuotas.
        if len(muestras) >= 30:
            xs = [m[0] for m in muestras]
            ys = [m[1] for m in muestras]
            r, err = correlacion(xs, ys)
            print(f"\n  (con las {len(muestras)} casillas que sí tienen")
            print(f"   cuotas, la correlación brecha ↔ desvío del público")
            print(f"   frente al mercado es {r:+.3f} ± {err:.3f})")

    # ------------------------------------------------------------------
    # A. ¿Predice la brecha el resultado, más allá de Elo y forma?
    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("A · ¿PREDICE MEJOR EL RESULTADO?")
    print("=" * 66)

    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    X, y, meta, _ = caracteristicas.construir(filas)
    Xa, ya = np.array(X), np.array(y)
    i_elo = caracteristicas.NOMBRES.index("elo_diferencia")
    i_forma = caracteristicas.NOMBRES.index("forma_diferencia")
    elo = Xa[:, i_elo]
    forma = Xa[:, i_forma]

    # La resta elo − forma NO sirve para probar nada: es una combinación
    # lineal de dos columnas que el modelo ya tiene, y un modelo lineal no
    # puede sacar nada de eso. Es colineal, el espacio de coeficientes es el
    # mismo. La primera versión de este script cometía ese error y por eso
    # daba +0,0001, que era ruido numérico y no un resultado.
    #
    # Lo que la regresión no puede expresar es un PRODUCTO. Se prueban dos:
    #
    #   producto    elo × forma, la interacción sin más
    #   asimétrico  la idea concreta: "equipo fuerte EN MALA FORMA es
    #               desproporcionadamente vulnerable", que solo se activa
    #               cuando se dan las dos condiciones a la vez, y su
    #               simétrica para el visitante
    producto = (elo * forma).reshape(-1, 1)

    fuerte_mal = np.maximum(elo, 0) * np.maximum(-forma, 0)
    debil_bien = np.maximum(-elo, 0) * np.maximum(forma, 0)
    asimetrico = np.column_stack([fuerte_mal, debil_bien])

    corte = int(len(Xa) * 0.80)
    print(f"\n  {corte} para entrenar, {len(Xa) - corte} para comprobar")
    print("  (corte por fecha, nunca al azar)")

    def perdida(datos):
        m = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=2000, C=0.5))
        m.fit(datos[:corte], ya[:corte])
        p = m.predict_proba(datos[corte:])
        t = ya[corte:]
        return float(-np.mean(np.log(np.clip(p[np.arange(len(t)), t], 1e-9, 1))))

    ll_sin = perdida(Xa)
    ll_resta = perdida(np.hstack([Xa, (elo - forma).reshape(-1, 1)]))
    ll_prod = perdida(np.hstack([Xa, producto]))
    ll_asim = perdida(np.hstack([Xa, asimetrico]))

    # Cuántos partidos activan de verdad la interacción asimétrica. Si son
    # pocos, una mejora pequeña en el promedio de todos puede ser una mejora
    # grande donde actúa, o puede ser nada: sin este número no se distingue.
    activos = int((fuerte_mal > 0).sum() + (debil_bien > 0).sum())
    print(f"\n  la interacción asimétrica se activa en {activos} de "
          f"{len(Xa)} partidos ({100 * activos / len(Xa):.0f}%)")

    print(f"\n  {'variante':<32} {'log-loss':>9} {'gana':>8}")
    print(f"  {'sin nada (como está)':<32} {ll_sin:>9.4f}")
    print(f"  {'resta elo − forma':<32} {ll_resta:>9.4f} "
          f"{ll_sin - ll_resta:>+8.4f}")
    print(f"  {'producto elo × forma':<32} {ll_prod:>9.4f} "
          f"{ll_sin - ll_prod:>+8.4f}")
    print(f"  {'fuerte-en-mala-forma (asim.)':<32} {ll_asim:>9.4f} "
          f"{ll_sin - ll_asim:>+8.4f}")

    print()
    print("  La fila de la RESTA tiene que dar cero o casi: es una")
    print("  combinación lineal de dos columnas que el modelo ya tiene, y")
    print("  un modelo lineal no puede sacar nada de eso. Está puesta como")
    print("  control: si esa fila diera algo grande, el test estaría mal.")

    mejor = max(ll_sin - ll_prod, ll_sin - ll_asim)
    print()
    if mejor > 0.002:
        print(f"  La interacción aporta {mejor:+.4f}. Es lo que la regresión")
        print("  no podía expresar por sí sola.")
    else:
        print("  Ninguna interacción aporta al resultado. Era lo esperable:")
        print("  el mercado ya descuenta la mala racha del favorito, así que")
        print("  eso no es información nueva para saber quién gana.")
        print("  No dice nada sobre la pregunta B, que es la que importa.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
