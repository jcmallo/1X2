"""
Análisis de la jornada por Claude: la capa que lee lo que los números no ven.

Qué hace y qué no
-----------------

El modelo estadístico calcula probabilidades a partir de 4.400 resultados. Es
bueno en eso y una IA no lo mejoraría: pedirle a un modelo de lenguaje que
estime un 43% es cambiar aritmética por intuición.

Lo que sí puede hacer, y el cálculo no, es LEER. Que a un equipo le falten
tres titulares, que llueva en San Mamés contra un equipo del sur, que haya
Champions el miércoles, que un entrenador esté a punto de caer: nada de eso
está en la tabla de resultados y todo eso mueve un partido.

Así que este script no sustituye al optimizador. Le pasa a Claude TODO lo que
el sistema sabe —lo que juega la gente, lo que dicen las casas, lo que dice
nuestro modelo, el valor de cada signo, el clima previsto— le deja buscar en
la web el contexto que falta, y le pide ajustes justificados uno a uno.

La regla que lo hace útil en vez de decorativo
----------------------------------------------

Cada ajuste va con su razón escrita, y todo se guarda. Dentro de unos meses
se podrá comprobar si los cambios de la IA acertaron más que dejar el cálculo
como estaba. Si no, se quita.

Sin esa medición esto sería una bola de cristal cara. Con ella, es una
hipótesis comprobable.

Coste
-----

Unos 5 EUR por jornada: tres análisis en paralelo (casillas 1-5, 6-10 y
11-14 más el Pleno, cada uno con hasta 16 búsquedas web) y una revisión
crítica. Antes era una sola llamada de unos 1,90 EUR que solo miraba las
casillas con más discrepancia y dejaba el resto sin leer.

Uso
---

    python -m ia.analizar --dry-run
    python -m ia.analizar --sin-web        (más barato, sin buscar lesiones)
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api_client import ApiIngesta  # noqa: E402

import anthropic  # noqa: E402


MODELO = "claude-opus-5"

# Cuánta discrepancia entre mercado, público y modelo hace que un partido
# merezca una búsqueda a fondo. Calibrado para que salgan entre tres y seis
# de los catorce: menos deja fuera partidos interesantes, más diluye la
# atención y hace que el modelo invente contexto donde no lo hay.
UMBRAL_ATENCION = 0.35

# Cuántas casillas van a la lista de atención como mucho. La primera
# ejecución marcó ocho, el modelo agotó las búsquedas en la segunda y dejó
# cinco sin mirar —incluidas las tres de Liga F, que son las de mayor
# discrepancia y menor cobertura de prensa—. Más vale investigar seis a
# fondo que ocho a medias.
MAX_ATENCION = 15  # ya no se recorta: se leen los 14 partidos y el Pleno

# El análisis se reparte en lotes que corren en paralelo. Cada lote ve la
# tabla completa (para comparar) pero solo investiga sus casillas, con su
# propio presupuesto de búsquedas. Así cada partido recibe ~3 búsquedas en vez
# de que 8 se queden sin mirar por agotarse el cupo.
LOTES = [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12, 13, 14, 15]]
BUSQUEDAS_REVISION = 12

# Un ajuste solo se aplica al boleto si su confianza llega a este nivel.
# Los de confianza inferior se guardan aparte ("ajustes_baja_confianza") para
# poder medirlos después, pero no mueven el boleto.
CONFIANZA_MINIMA = "media"
_NIVEL = {"baja": 0, "media": 1, "alta": 2}

# Búsquedas web permitidas. A unos 8.000 tokens de resultados cada una, es
# lo que domina la factura: pasar de 10 a 20 no dobla el coste del modelo,
# pero sí llena el contexto. Dos por casilla marcada, más margen.
MAX_BUSQUEDAS = 16  # por lote
PRECIO_BUSQUEDA = 0.01  # USD por búsqueda, aparte de los tokens

# Cuándo analizar: cinco horas antes del primer partido de la jornada.
#
# No antes: a 24 horas no hay alineaciones probables, muchas ruedas de prensa
# no se han dado y la previsión del tiempo es bastante peor. Se pagaría por
# leer una prensa que aún no ha publicado lo que importa.
#
# No después: la quiniela cierra minutos antes del primer partido, y hay que
# dejar margen para sellar el boleto. En la ejecución del 5 de septiembre el
# análisis llegó con cuatro partidos ya empezados.
#
# La ventana es de media hora a cada lado porque el cron de GitHub no es
# puntual: puede retrasarse unos minutos cuando hay cola.
HORAS_ANTES = 5.0
MARGEN_HORAS = 0.5

# Los horarios de los partidos se guardan en hora española, sin zona. El
# runner de GitHub va en UTC, así que hay que decirlo explícitamente o el
# cálculo se va dos horas en verano.
ZONA = ZoneInfo("Europe/Madrid")

# Precios por millón de tokens, para poder registrar lo que cuesta cada
# ejecución en lugar de estimarlo.
PRECIO_ENTRADA = 5.00
PRECIO_SALIDA = 25.00


SISTEMA = """Analizas jornadas de La Quiniela para un sistema que ya hace sus
propios cálculos. NO recalculas probabilidades: eso lo hace un modelo
estadístico entrenado con 4.402 partidos, y lo hace mejor que tú.

Tu trabajo es convertir información humana en datos medibles, y revisar
TODOS los partidos que te asignen: ninguno se queda sin leer.

LA PREGUNTA QUE IMPORTA

No es "¿es importante que este jugador no juegue?".

Es "¿esta información YA está metida en la cuota?".

Si un titular cae lesionado a las 11:08 y la cuota del local pasa del 47% al
41% a las 12:00, el mercado ya lo ha descontado. Si tú lo "descubres" a las
13:45 y vuelves a rebajar al local, estás contando dos veces lo mismo y
empeorando la estimación.

Por eso recibes el MOVIMIENTO de las cuotas por franja horaria, no solo su
último valor. Úsalo. Antes de proponer nada, comprueba si el precio ya se
movió en esa dirección.

QUÉ TIENES DELANTE

  % LAE        cuánta gente juega cada signo. Qué hace el público, no qué
               va a pasar.
  % Apuestas   probabilidad implícita en las cuotas, sin margen. Es el mejor
               estimador público: hay dinero detrás.
  % Predictor  el modelo propio: Elo, forma, goles, descanso, directos.
               Un guion significa que no supera al mercado en esa liga.
  Valor        Apuestas ÷ LAE. Por encima de 1 el signo está infrajugado.
  Movimiento   cómo han evolucionado cuotas y porcentajes por franja.

CÓMO USAR EL % PREDICTOR

El Predictor NO lee noticias: no sabe de lesiones, rotaciones, cambios de
entrenador ni sanciones. Sí sabe de rendimiento acumulado (Elo, forma, goles).
Por eso su discrepancia con las cuotas es una HIPÓTESIS que hay que contrastar
con la prensa, no un veredicto:

  - Si Predictor y mercado discrepan en 8 puntos o más sobre algún signo,
    busca de forma explícita qué podría explicarlo (bajas, rotaciones,
    racha, calendario). Di si la prensa da la razón al Predictor, al mercado
    o a ninguno.
  - Si la prensa confirma lo que dice el Predictor y el mercado aún no lo
    refleja (NOVEDAD), es el mejor caso para un ajuste hacia el Predictor.
  - Si la prensa explica por qué el Predictor se equivoca (p. ej. baja
    reciente que su histórico no ve), dilo: el ajuste va hacia el mercado.
  - Si coinciden los tres, la casilla es de bajo riesgo: basta confirmar con
    una búsqueda que no hay una baja de última hora.
  - Sin Predictor (guion), manda el mercado; en Liga F el mercado es una
    cuota casi congelada de 1xBet, así que ahí la prensa pesa más.

QUÉ DEVUELVES: SEÑALES, NO PORCENTAJES

Prohibido decir "el Atlético baja al 41%". Eso es intuición disfrazada de
número.

Lo que sirve es describir el HECHO con precisión y dejar que la estadística
aprenda cuánto vale:

  titulares_ausentes: 2          quién falta importa más que cuántos
  delantero_referencia_ausente: 1
  portero_titular_ausente: 0
  calidad_sustituto: alta|media|baja
  titulares_que_vuelven: 1       las altas cuentan tanto como las bajas
  jugo_entre_semana: si|no
  minutos_acumulados_altos: si|no
  cambio_entrenador_reciente: si|no
  historico_representativo: si|no    ¿siguen siendo el mismo equipo?
  clima_adverso: si|no
  clima_favorece: local|visitante|ninguno
  algo_en_juego: ambos|solo_local|solo_visitante|ninguno

Fíjate en el sesgo que hay que evitar: si solo buscas "lesionados", solo
encontrarás malas noticias. Busca también quién VUELVE.

FIABILIDAD DE LA FUENTE

Cada hecho lleva su nivel, porque no vale lo mismo un comunicado del club
que un rumor de foro:

  A  comunicado oficial, convocatoria, alineación publicada, rueda de prensa
  B  periodista local acreditado, medio especializado en alineaciones
  C  prensa deportiva generalista
  D  redes sociales, foros, rumor sin firma

Y su estado respecto al mercado:

  NOVEDAD          publicado después de la última captura de cuotas
  YA_CONOCIDA      publicado antes, pero el precio no se movió
  DESCONTADA       publicado antes y el precio SÍ se movió en esa dirección

Una señal DESCONTADA no debe generar ajuste. Regístrala igual: sirve para
comprobar después si el mercado reacciona bien o mal a cada tipo de noticia.

CÓMO BUSCAR

Para cada partido asignado busca a AMBOS equipos y, además de la prensa
nacional, la prensa local de cada uno (el diario de su ciudad, el medio del
club, la rueda de prensa del entrenador). Es donde aparecen antes las
rotaciones, los pequeños problemas físicos y el estado de ánimo del vestuario.
Mira también forma reciente (últimos 5), calendario (Champions, Copa,
selecciones), sanciones, entrenador en el alero y qué se juega cada uno.

Busca por lo que necesitas saber, no por dónde crees que estará. Una consulta
como "Getafe Celta lesionados alineación probable" encuentra mejores fuentes
que ir a un periódico concreto: los sitios de fantasy y estadística tienen
partes médicos más completos que la prensa generalista, porque es su producto.

Incluye siempre la fecha o la jornada. Sin eso salen noticias de temporadas
pasadas, y una lesión de hace meses ya está en el precio.

Para el tiempo, la previsión de la ciudad A LA HORA DEL PARTIDO, no del día.

Liga F tiene mucha menos cobertura. Si no encuentras nada de un partido
femenino, dilo: es información en sí misma.

REGLAS

1. Todo ajuste lleva una razón concreta y verificable. "Intuición" o "suele
   ganar" no son razones. Pero no uses esa regla para esconderte: tienes que
   mirar TODOS los partidos asignados con el mismo esfuerzo y concluir en
   cada uno. Si tras buscar en ambos equipos y en la prensa local no hay
   nada, cuenta qué buscaste y qué encontraste, y mantén; no inventes un
   cambio para tener algo que mostrar. Pero si hay un signo mejor, dilo.

2. Si el precio ya se movió, la información ya está contada. No la cuentes
   otra vez.

3. Prioriza el VALOR sobre la probabilidad. El premio se reparte entre
   acertantes: acertar lo que acierta todo el mundo paga poco.

4. No inventes. Si buscas y no encuentras, dilo.

5. Máximo 600 palabras de análisis (cuentan por partido unas dos líneas).

6. Un ajuste debe indicar si va en la dirección del Predictor, del mercado
   o de ninguno de los dos, y por qué.

CÓMO RESPONDER

Análisis breve, y después este JSON:

```json
{
  "senales": [
    {"posicion": 3,
     "hechos": {"titulares_ausentes": 2, "delantero_referencia_ausente": 1,
                "calidad_sustituto": "baja", "clima_adverso": true},
     "fuente_nivel": "A",
     "estado_mercado": "NOVEDAD|YA_CONOCIDA|DESCONTADA",
     "resumen": "qué has encontrado, en una frase"}
  ],
  "ajustes": [
    {"posicion": 3, "signo_antes": "1", "signo_despues": "X",
     "confianza": "alta|media|baja",
     "razon": "por qué, y por qué el mercado no lo ha descontado ya"}
  ],
  "casillas": [
    {"posicion": 3, "veredicto": "mantener|cambiar",
     "resumen": "qué has leído de ambos equipos, en una o dos frases",
     "frente_a_predictor": "coincide|discrepa|sin_predictor",
     "riesgo": "bajo|medio|alto"}
  ],
  "pleno": {"local": "1", "visitante": "0", "razon": "..."},
  "aviso": "lo que el sistema debería saber y no cabe arriba"
}
```

Incluye una entrada en "casillas" por cada posición que te toque, sin
saltarte ninguna. Una señal registrada sirve para medir después aunque hoy no
cambie nada."""


CRITICO = """Eres el segundo par de ojos. Otro analista ha propuesto ajustes
sobre una jornada de quiniela y tu trabajo es intentar tumbarlos.

El fallo típico de un análisis así es construir una historia convincente a
partir de evidencia débil: tres datos sueltos, una noticia vieja y una
conclusión que suena bien. Tú estás para detectar eso.

Por cada ajuste propuesto, comprueba:

  - ¿La noticia es reciente, o de hace semanas?
  - ¿El precio ya se movió en esa dirección? Si sí, ya está contado.
  - ¿La baja era conocida desde hace días?
  - ¿El sustituto juega habitualmente? Entonces no es una baja de verdad.
  - ¿La fuente es un comunicado o un rumor?
  - ¿Hay fuentes que lo contradigan?
  - ¿El razonamiento se sostiene, o encadena suposiciones?
  - ¿Qué dice el % Predictor? Si el ajuste va contra el Predictor y contra
    el mercado a la vez, necesita evidencia fuerte (nivel A o B). Si va a
    favor del Predictor y la noticia es nueva, es plausible.

LOS TRES VEREDICTOS NO SON UNA ESCALA DE SEVERIDAD

Son tres cosas distintas, y confundirlas deja pasar ajustes que no deberían:

  descartar   la premisa es FALSA. La noticia dice lo contrario de lo que el
              análisis entendió, la fecha no es la que cree, el jugador no
              existe, el precio ya se había movido. Un hecho mal leído no se
              rebaja: se tira. Da igual que la conclusión pudiera ser
              correcta por casualidad.

  rebajar     la premisa es CIERTA pero la evidencia es floja: una sola
              fuente de nivel C, una inferencia larga, un efecto real pero
              probablemente pequeño.

  mantener    el ajuste se sostiene.

Si al escribir tu resumen usas palabras como "malinterpretado", "no es lo
que dice la fuente", "de hace meses" o "inexistente", el veredicto de ese
ajuste es "descartar". No "rebajar". Comprueba que tus veredictos dicen lo
mismo que tu resumen: es el error más fácil de cometer aquí.

No busques equilibrio ni des el visto bueno por cortesía. Si un ajuste está
bien fundado, dilo en una línea y sigue. Tu utilidad está en los que no lo
están.

Responde con este JSON:

```json
{
  "veredictos": [
    {"posicion": 3, "veredicto": "mantener|descartar|rebajar",
     "motivo": "por qué"}
  ],
  "resumen": "una frase sobre la calidad general del análisis"
}
```"""


def horas_hasta_el_cierre(datos: dict) -> float | None:
    """
    Cuánto falta para el primer partido, que es cuando cierra la quiniela.

    Devuelve None si ninguna casilla tiene hora, que pasa cuando aún no se ha
    vinculado el boleto con los partidos.
    """
    horas = []
    ahora = datetime.now(ZONA)
    for c in datos.get("casillas", []):
        crudo = c.get("fecha_hora_inicio")
        if not crudo:
            continue
        try:
            inicio = datetime.fromisoformat(str(crudo)).replace(tzinfo=ZONA)
        except ValueError:
            continue
        horas.append((inicio - ahora).total_seconds() / 3600)
    return min(horas) if horas else None


def reunir_contexto(api: ApiIngesta, temporada: str, jornada: int) -> tuple[dict, dict]:
    """
    Todo lo que el sistema sabe de la jornada, y cómo ha ido cambiando.

    El movimiento va aparte porque responde a otra pregunta: no "cuánto vale
    este signo" sino "cuándo se movió el precio", que es lo que permite saber
    si una noticia ya está descontada.
    """
    datos = api.contexto_dashboard(
        temporada=temporada or None,
        numero_jornada=jornada or None,
    )
    try:
        mov = api.contexto_movimiento(
            temporada=temporada or None,
            numero_jornada=jornada or None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  (sin movimiento de cuotas: {exc})")
        mov = {}
    return datos, mov


def prioridad(terna_lae, terna_mer, terna_mod) -> float:
    """
    Cuánta atención merece esta casilla.

    No todos los partidos valen lo mismo. Donde el mercado, el público y
    nuestro modelo coinciden no hay nada que investigar: buscar noticias
    igualmente solo añade coste y tienta al modelo a encontrar historias
    donde no las hay.

    Donde discrepan, en cambio, alguien sabe algo que los demás no. Ahí es
    donde una búsqueda puede aportar.
    """
    p = 0.0
    if terna_mer and terna_mod:
        # El modelo contra el mercado: la discrepancia más informativa.
        p += sum(abs(a - b) for a, b in zip(terna_mer, terna_mod)) * 1.5
    if terna_mer and terna_lae:
        # El público contra el mercado: donde está el valor.
        p += sum(abs(a - b) for a, b in zip(terna_mer, terna_lae))
    if not terna_mer:
        # Sin cuotas no hay red de seguridad: conviene mirar.
        p += 0.5
    return p


def formatear(datos: dict, movimiento: dict | None = None) -> str:
    """
    El estado de la jornada como texto legible.

    Se manda como texto y no como JSON crudo porque el modelo razona mejor
    sobre una tabla que sobre llaves anidadas, y porque así el prompt es
    legible cuando haya que depurar por qué propuso algo raro.
    """
    j = datos["jornada"]
    lineas = [
        f"JORNADA {j['numero']} · {j['temporada']}",
        "",
        f"{'#':>2}  {'partido':<40} {'comp':<10} "
        f"{'LAE':>12} {'Apuestas':>12} {'Predictor':>12} {'valor':>6}",
        "-" * 100,
    ]

    def terna(d, a="p1", b="px", c="p2"):
        if not d:
            return None
        try:
            v = [float(d[a]), float(d[b]), float(d[c])]
        except (KeyError, TypeError, ValueError):
            return None
        t = sum(v)
        return [x / t for x in v] if t > 0 else None

    atencion: list[tuple[float, int, str]] = []

    for c in sorted(datos.get("casillas", []), key=lambda x: int(x["posicion"])):
        pos = int(c["posicion"])
        pr = c["probabilidades"] if isinstance(c["probabilidades"], dict) else {}

        if pos == 15:
            lineas.append("")
            lineas.append(
                f"15. PLENO AL 15: {c['local']} - {c['visitante']}"
                f"  ({c.get('fecha_hora_inicio') or 'sin hora'})"
            )
            lineas.append("    Se juega a goles de cada equipo: 0, 1, 2 o M (tres o más).")
            p15 = datos.get("pleno15") or {}
            for fuente, lados in p15.items():
                for lado, v in lados.items():
                    lineas.append(
                        f"    {fuente:<14} {lado:<10} "
                        f"0:{v['p0']:.0%}  1:{v['p1']:.0%}  "
                        f"2:{v['p2']:.0%}  M:{v['pm']:.0%}"
                    )
            continue

        lae = terna(pr.get("LAE_CIERRE") or pr.get("LAE_ESTIMADO"))
        mer = terna(c.get("mercado"))
        mod = terna(pr.get("MODELO_PROPIO"))
        prop = (c.get("propuesta") or {}).get("signos", [])

        def fmt(t):
            return f"{t[0]*100:>3.0f}/{t[1]*100:>3.0f}/{t[2]*100:>3.0f}" if t else "     —     "

        valor = ""
        if mer and lae and prop:
            idx = {"1": 0, "X": 1, "2": 2}
            vs = [mer[idx[s]] / max(lae[idx[s]], 0.005) for s in prop if s in idx]
            if vs:
                valor = f"{max(vs):.2f}"

        p = prioridad(lae, mer, mod)
        if True:  # ya se leen todas; la prioridad solo ordena
            atencion.append((p, pos, f"{c['local']} - {c['visitante']}"))

        lineas.append(
            f"{pos:>2}{'*' if p >= UMBRAL_ATENCION else ' '} "
            f"{(c['local'][:18] + ' - ' + c['visitante'][:18]):<40} "
            f"{(c.get('competicion') or '')[:9]:<10} "
            f"{fmt(lae):>12} {fmt(mer):>12} {fmt(mod):>12} {valor:>6}"
            f"   propuesta: {''.join(prop) or '—'}"
            f"   {c.get('fecha_hora_inicio') or ''}"
        )

    lineas += [
        "",
        "Las ternas van en orden 1/X/2. Un guion significa que ese dato no",
        "existe para esa casilla, no que sea cero.",
    ]

    # --- Dónde merece la pena gastar búsquedas --------------------------
    #
    # Catorce partidos son demasiados para investigarlos todos a fondo, y
    # además sería contraproducente: buscar noticias de un partido donde
    # todo el mundo coincide solo sirve para encontrar historias donde no
    # las hay. Donde el mercado, el público y el modelo discrepan, en
    # cambio, alguien sabe algo.
    if atencion:
        atencion.sort(reverse=True)
        del atencion[MAX_ATENCION:]
        lineas += [
            "",
            "",
            "ORDEN DE DISCREPANCIA (mayor primero)",
            "",
            "Se analizan todos los partidos. Las marcadas con * arriba son",
            "aquellas donde mercado, público y Predictor no se ponen de",
            "acuerdo: dedícales más búsquedas dentro de tu cupo.",
            "",
        ]
        for p, pos, nombre in atencion:
            lineas.append(f"  {pos:>2}. {nombre:<44} discrepancia {p:.2f}")

    # --- Cómo se ha movido el precio ------------------------------------
    #
    # Esta es la parte que permite no contar dos veces la misma noticia.
    mov = (movimiento or {}).get("casillas") or []
    con_movimiento = [
        m for m in mov
        if len({(x.get("franja"), round(x.get("p1") or 0, 3)) for x in m.get("mercado", [])}) > 1
    ]
    if con_movimiento:
        lineas += [
            "",
            "",
            "MOVIMIENTO DE LAS CUOTAS Y DEL PÚBLICO",
            "",
            "Si el precio ya se movió en la dirección de una noticia, esa",
            "noticia ya está contada. No la cuentes otra vez.",
            "",
        ]
        for m in con_movimiento:
            lineas.append(f"{m['posicion']:>2}. {m['local']} - {m['visitante']}")
            for fila, etiqueta in ((m.get("mercado", []), "mercado"),
                                   (m.get("publico", []), "público")):
                for x in fila:
                    if x.get("p1") is None:
                        continue
                    lineas.append(
                        f"      {etiqueta:<8} {str(x.get('franja') or '?'):<8} "
                        f"{x['p1']*100:>3.0f}/{x['px']*100:>3.0f}/{x['p2']*100:>3.0f}"
                        f"   {x.get('capturado_en') or ''}"
                    )
            lineas.append("")

    return "\n".join(lineas)


def analizar(contexto: str, con_web: bool, lote: list[int] | None = None) -> tuple[str, dict, dict]:
    """Le pasa la jornada a Claude y devuelve su análisis y sus ajustes."""
    cliente = anthropic.Anthropic()

    herramientas = []
    if con_web:
        # Sin buscar, el modelo solo puede razonar sobre los números que ya
        # tiene, que es justo lo que no aporta nada nuevo. Las bajas y el
        # clima hay que ir a buscarlos.
        herramientas.append({
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": MAX_BUSQUEDAS,
        })

    asignadas = lote or list(range(1, 16))
    nombre_lote = ", ".join(str(x) for x in asignadas)
    mensaje = cliente.messages.create(
        model=MODELO,
        max_tokens=16000,
        system=SISTEMA,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        tools=herramientas or anthropic.NOT_GIVEN,
        messages=[{
            "role": "user",
            "content": (
                f"{contexto}\n\n"
                f"Analiza ESTAS casillas: {nombre_lote} (la 15 es el Pleno). "
                "El resto las ve otro analista; úsalas solo para comparar. "
                f"Tienes {MAX_BUSQUEDAS} búsquedas: repártelas entre tus "
                "casillas, más a las de mayor discrepancia con el Predictor. "
                "Busca bajas y vueltas, rotaciones, forma, prensa local de "
                "ambos equipos, el tiempo en la sede y cualquier circunstancia "
                "que mueva un partido y no esté en los números.\n\n"
                "Devuelve una entrada en \"casillas\" por cada posición "
                "asignada. Contrasta siempre con el % Predictor."
            ),
        }],
    )

    # Un rechazo por seguridad llega con HTTP 200: hay que mirar stop_reason
    # antes de leer el contenido.
    if mensaje.stop_reason == "refusal":
        raise RuntimeError(
            "Claude ha declinado la petición"
            + (f": {mensaje.stop_details.explanation}" if mensaje.stop_details else "")
        )

    texto = "".join(b.text for b in mensaje.content if b.type == "text")

    ajustes = {}
    if "```json" in texto:
        crudo = texto.split("```json", 1)[1].split("```", 1)[0]
        try:
            ajustes = json.loads(crudo)
        except json.JSONDecodeError as exc:
            print(f"  AVISO: el bloque JSON no se pudo leer ({exc}).")

    busquedas = sum(
        1 for b in mensaje.content if b.type == "server_tool_use"
    )
    uso = {
        "tokens_entrada": mensaje.usage.input_tokens,
        "tokens_salida": mensaje.usage.output_tokens,
        # Las búsquedas se cobran aparte de los tokens que devuelven. Sin
        # esta línea el coste que se guarda es un 20% menor que el real, y
        # entonces la decisión de "¿compensa esta capa?" se toma con una
        # cifra falsa.
        "coste_usd": round(
            mensaje.usage.input_tokens / 1e6 * PRECIO_ENTRADA
            + mensaje.usage.output_tokens / 1e6 * PRECIO_SALIDA
            + busquedas * PRECIO_BUSQUEDA,
            5,
        ),
        "busquedas_web": busquedas,
    }
    return texto, ajustes, uso


def revisar(contexto: str, texto: str, ajustes: dict) -> tuple[dict, dict]:
    """
    Un segundo modelo intenta tumbar los ajustes del primero.

    Un análisis de este tipo falla casi siempre igual: construye una historia
    convincente a partir de evidencia floja. Tres datos sueltos, una noticia
    de hace tres semanas y una conclusión que suena razonable. El que la
    escribe no lo ve, porque acaba de convencerse a sí mismo.

    Un segundo modelo que no ha pasado por ese razonamiento sí lo ve. Y
    cuesta unos céntimos: menos que la diferencia entre acertar y no.
    """
    lista = ajustes.get("ajustes") or []
    if not lista:
        return {}, {"tokens_entrada": 0, "tokens_salida": 0, "coste_usd": 0.0,
                    "busquedas_web": 0}

    cliente = anthropic.Anthropic()
    mensaje = cliente.messages.create(
        model=MODELO,
        max_tokens=8000,
        system=CRITICO,
        thinking={"type": "adaptive"},
        tools=[{"type": "web_search_20260209", "name": "web_search",
                "max_uses": BUSQUEDAS_REVISION}],
        messages=[{
            "role": "user",
            "content": (
                f"{contexto}\n\n"
                f"--- ANÁLISIS A REVISAR ---\n\n{texto}\n\n"
                "Intenta tumbar cada ajuste. Comprueba las fechas de las "
                "noticias y si el precio ya se había movido."
            ),
        }],
    )

    if mensaje.stop_reason == "refusal":
        return {}, {"tokens_entrada": 0, "tokens_salida": 0, "coste_usd": 0.0,
                    "busquedas_web": 0}

    salida = "".join(b.text for b in mensaje.content if b.type == "text")
    veredictos = {}
    if "```json" in salida:
        try:
            veredictos = json.loads(
                salida.split("```json", 1)[1].split("```", 1)[0]
            )
        except json.JSONDecodeError:
            pass
    veredictos["_texto"] = salida

    busquedas_rev = sum(1 for b in mensaje.content if b.type == "server_tool_use")
    uso = {
        "tokens_entrada": mensaje.usage.input_tokens,
        "tokens_salida": mensaje.usage.output_tokens,
        "coste_usd": round(
            mensaje.usage.input_tokens / 1e6 * PRECIO_ENTRADA
            + mensaje.usage.output_tokens / 1e6 * PRECIO_SALIDA
            + busquedas_rev * PRECIO_BUSQUEDA, 5),
        "busquedas_web": busquedas_rev,
    }
    return veredictos, uso


def aplicar_veredictos(ajustes: dict, veredictos: dict) -> dict:
    """
    Se queda solo con los ajustes que aguantan la revisión.

    Descartar es descartar. Rebajar mantiene el ajuste pero le baja la
    confianza, que es lo que decide después si el optimizador se fía.
    """
    v = {int(x["posicion"]): x for x in (veredictos.get("veredictos") or [])
         if isinstance(x, dict) and "posicion" in x}
    if not v:
        return ajustes

    # Si el crítico escribe que la premisa es falsa pero vota "rebajar", el
    # ajuste sobrevive con confianza baja y acaba en el boleto igual. Pasó en
    # la primera ejecución real: dijo que una noticia de catorce meses se
    # había leído al revés, y aun así lo dejó pasar.
    DELATORAS = (
        "malinterpret", "al revés", "no es lo que dice", "inexistente",
        "no existe", "hace meses", "de hace un año", "contradice",
        "falso", "erróne", "confunde",
    )

    orden = {"alta": "media", "media": "baja", "baja": "baja"}
    sobreviven = []
    for a in ajustes.get("ajustes") or []:
        pos = int(a.get("posicion", 0))
        veredicto = (v.get(pos) or {}).get("veredicto", "mantener")

        motivo = ((v.get(pos) or {}).get("motivo", "")
                  + " " + str(veredictos.get("resumen", ""))).lower()
        if veredicto != "descartar" and any(d in motivo for d in DELATORAS):
            print(
                f"  casilla {pos}: el crítico votó '{veredicto}' pero su "
                "motivo dice que la premisa es falsa. Se descarta."
            )
            veredicto = "descartar"

        if veredicto == "descartar":
            a["descartado_por_revision"] = (v[pos] or {}).get("motivo", "")
            ajustes.setdefault("descartados", []).append(a)
            continue
        if veredicto == "rebajar":
            a["confianza"] = orden.get(a.get("confianza", "media"), "baja")
            a["rebajado_por_revision"] = (v[pos] or {}).get("motivo", "")
        sobreviven.append(a)

    ajustes["ajustes"] = sobreviven
    ajustes["revision"] = veredictos
    return ajustes


def fusionar(resultados: list[tuple[str, dict, dict]]) -> tuple[str, dict, dict]:
    """Une los análisis de los lotes en uno solo."""
    texto = "\n\n".join(t for t, _, _ in resultados if t)
    fusion: dict = {"senales": [], "ajustes": [], "casillas": [], "avisos": []}
    pleno = None
    for _, a, _ in resultados:
        if not isinstance(a, dict):
            continue
        fusion["senales"] += a.get("senales") or []
        fusion["ajustes"] += a.get("ajustes") or []
        fusion["casillas"] += a.get("casillas") or []
        if a.get("aviso"):
            fusion["avisos"].append(a["aviso"])
        if a.get("pleno") and pleno is None:
            pleno = a["pleno"]
    if pleno:
        fusion["pleno"] = pleno
    fusion["aviso"] = " | ".join(fusion.pop("avisos"))
    uso = {"tokens_entrada": 0, "tokens_salida": 0, "coste_usd": 0.0,
           "busquedas_web": 0}
    for _, _, u in resultados:
        for k in uso:
            uso[k] += u[k]
    uso["coste_usd"] = round(uso["coste_usd"], 5)
    return texto, fusion, uso


def separar_por_confianza(ajustes: dict) -> dict:
    """
    Solo los ajustes con confianza suficiente llegan al boleto.

    El panel aplica todo lo que haya en "ajustes". Los de confianza baja se
    guardan en "ajustes_baja_confianza": se pueden medir después sin que
    muevan el dinero hoy.
    """
    minimo = _NIVEL[CONFIANZA_MINIMA]
    firmes, flojos = [], []
    for a in ajustes.get("ajustes") or []:
        nivel = _NIVEL.get(str(a.get("confianza", "baja")).lower(), 0)
        (firmes if nivel >= minimo else flojos).append(a)
    ajustes["ajustes"] = firmes
    if flojos:
        ajustes["ajustes_baja_confianza"] = flojos
    return ajustes


def main() -> int:
    p = argparse.ArgumentParser(description="Analiza la jornada con Claude.")
    p.add_argument("--temporada", default="")
    p.add_argument("--jornada", type=int, default=0)
    p.add_argument("--tipo", default="T-24", help="franja temporal")
    p.add_argument(
        "--sin-web", action="store_true",
        help="no buscar en internet: más barato, pero sin bajas ni clima",
    )
    p.add_argument(
        "--solicitud", type=int, default=0,
        help="id del lanzamiento pedido desde el panel; cierra su candado",
    )
    p.add_argument(
        "--forzar", action="store_true",
        help="analizar aunque no sea la ventana de las 5 h",
    )
    p.add_argument(
        "--sin-revision", action="store_true",
        help="no pasar los ajustes por el segundo modelo",
    )
    p.add_argument("--dry-run", action="store_true", help="no guarda nada")
    args = p.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "Falta ANTHROPIC_API_KEY. Se añade como secreto del repositorio, "
            "igual que INGEST_API_TOKEN."
        )
        return 1

    api = ApiIngesta()
    datos, movimiento = reunir_contexto(api, args.temporada, args.jornada)
    if not datos.get("jornada"):
        print("No hay ninguna jornada cargada.")
        return 1

    j = datos["jornada"]
    print(f"Jornada {j['numero']} · {j['temporada']}")

    # --- ¿Es el momento? ------------------------------------------------
    #
    # Salir aquí no cuesta nada: no se ha llamado todavía a Claude ni se ha
    # buscado nada. Por eso el cron puede dispararse muchas veces al día sin
    # que importe: solo una cae dentro de la ventana.
    # Un lanzamiento pedido desde el panel ya pasó por la ventana de 12 horas
    # en lanzar_ia.php. Volver a comprobarla aquí con la de 5 lo rechazaría.
    if args.solicitud:
        args.forzar = True

    faltan = horas_hasta_el_cierre(datos)
    if faltan is None:
        print("  Ninguna casilla tiene hora todavía: falta vincular el boleto.")
        if not args.forzar:
            return 0
    elif not args.forzar:
        print(f"  Faltan {faltan:.1f} h para el primer partido.")
        if faltan > HORAS_ANTES + MARGEN_HORAS:
            print(
                f"  Aún es pronto: se analiza a {HORAS_ANTES:.0f} h del "
                "cierre, cuando ya hay alineaciones probables. No se gasta "
                "nada."
            )
            return 0
        if faltan < HORAS_ANTES - MARGEN_HORAS:
            print(
                "  Demasiado tarde: o la jornada está a punto de cerrar o ya "
                "hay partidos en juego. Analizar ahora sería pagar por "
                "noticias que ya no se pueden jugar."
            )
            return 0

    contexto = formatear(datos, movimiento)
    print(f"  contexto: {len(contexto)} caracteres\n")

    print(f"Consultando a {MODELO}" + (" con búsqueda web" if not args.sin_web else "") + "...")
    with ThreadPoolExecutor(max_workers=len(LOTES)) as pool:
        futuros = [
            pool.submit(analizar, contexto, not args.sin_web, lote)
            for lote in LOTES
        ]
        resultados = [f.result() for f in futuros]
    texto, ajustes, uso = fusionar(resultados)

    print(
        f"  {uso['tokens_entrada']:,} tokens de entrada, "
        f"{uso['tokens_salida']:,} de salida, "
        f"{uso['busquedas_web']} búsquedas · ${uso['coste_usd']:.4f}\n"
    )
    print(texto)

    if not args.sin_revision and isinstance(ajustes, dict) and ajustes.get("ajustes"):
        print("\nSegunda opinión...")
        veredictos, uso_rev = revisar(contexto, texto, ajustes)
        if veredictos:
            antes = len(ajustes.get("ajustes") or [])
            ajustes = aplicar_veredictos(ajustes, veredictos)
            despues = len(ajustes.get("ajustes") or [])
            print(f"  {antes} → {despues} ajustes · ${uso_rev['coste_usd']:.4f}")
            if veredictos.get("resumen"):
                print(f"  {veredictos['resumen']}")
            uso["coste_usd"] = round(uso["coste_usd"] + uso_rev["coste_usd"], 5)
            uso["tokens_entrada"] += uso_rev["tokens_entrada"]
            uso["tokens_salida"] += uso_rev["tokens_salida"]
            uso["busquedas_web"] += uso_rev["busquedas_web"]

    ajustes = separar_por_confianza(ajustes)
    cubiertas = {int(c.get("posicion", 0)) for c in ajustes.get("casillas") or []
                 if isinstance(c, dict)}
    faltan_casillas = [n for n in range(1, 15) if n not in cubiertas]
    if faltan_casillas:
        print(f"  AVISO: sin veredicto para las casillas {faltan_casillas}")
        ajustes["casillas_sin_cubrir"] = faltan_casillas

    senales = ajustes.get("senales", []) if isinstance(ajustes, dict) else []
    if senales:
        print(f"\n{len(senales)} señales encontradas")
        for x in senales:
            hechos = ", ".join(
                f"{k}={v}" for k, v in (x.get("hechos") or {}).items()
            )
            print(
                f"  casilla {x.get('posicion')}: [{x.get('fuente_nivel', '?')}] "
                f"{x.get('estado_mercado', '?')}  {hechos}"
            )

    lista = ajustes.get("ajustes", []) if isinstance(ajustes, dict) else []
    print(f"\n{len(lista)} ajustes propuestos")
    for a in lista:
        print(
            f"  casilla {a.get('posicion')}: {a.get('signo_antes')} → "
            f"{a.get('signo_despues')}  ({a.get('confianza')})"
        )

    if args.dry_run:
        print("\nDRY RUN: no se ha guardado nada.")
        return 0

    r = api.guardar_analisis_ia({
        "numero_jornada": int(j["numero"]),
        "etiqueta_temporada": j["temporada"],
        "modelo": MODELO,
        "tipo": args.tipo,
        "analisis": texto,
        "ajustes": json.dumps(ajustes, ensure_ascii=False),
        "solicitud_id": args.solicitud or "",
        "senales": json.dumps(
            ajustes.get("senales") or [], ensure_ascii=False
        ) if isinstance(ajustes, dict) else None,
        **uso,
    })
    print(f"\nGuardado: análisis {r.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
