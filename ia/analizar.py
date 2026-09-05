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

Unos 0,17 EUR por jornada con búsqueda web, 8,92 EUR al año. El boleto de una
sola jornada cuesta 6 EUR.

Uso
---

    python -m ia.analizar --dry-run
    python -m ia.analizar --sin-web        (más barato, sin buscar lesiones)
"""

from __future__ import annotations

import argparse
import json
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

# Precios por millón de tokens, para poder registrar lo que cuesta cada
# ejecución en lugar de estimarlo.
PRECIO_ENTRADA = 5.00
PRECIO_SALIDA = 25.00


SISTEMA = """Analizas jornadas de La Quiniela para un sistema que ya hace sus
propios cálculos. NO recalculas probabilidades: eso lo hace un modelo
estadístico entrenado con 4.402 partidos, y lo hace mejor que tú.

Tu trabajo es convertir información humana en datos medibles.

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
  Valor        Apuestas ÷ LAE. Por encima de 1 el signo está infrajugado.
  Movimiento   cómo han evolucionado cuotas y porcentajes por franja.

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

1. Sin razón concreta y verificable, no hay ajuste. "Intuición" o "suele
   ganar" no son razones. Cero ajustes es una respuesta válida y frecuente.

2. Si el precio ya se movió, la información ya está contada. No la cuentes
   otra vez.

3. Prioriza el VALOR sobre la probabilidad. El premio se reparte entre
   acertantes: acertar lo que acierta todo el mundo paga poco.

4. No inventes. Si buscas y no encuentras, dilo.

5. Máximo 400 palabras de análisis.

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
  "pleno": {"local": "1", "visitante": "0", "razon": "..."},
  "aviso": "lo que el sistema debería saber y no cabe arriba"
}
```

Puede haber señales sin ajuste: es lo normal y lo deseable. Una señal
registrada sirve para medir después aunque hoy no cambie nada."""


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
        if p >= UMBRAL_ATENCION:
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
        lineas += [
            "",
            "",
            "DÓNDE MIRAR PRIMERO",
            "",
            "Marcadas con * arriba. En estas casillas las tres fuentes no se",
            "ponen de acuerdo, así que hay algo que los números no explican.",
            "Busca a fondo en estas; en las demás, solo si te sobra margen.",
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


def analizar(contexto: str, con_web: bool) -> tuple[str, dict, dict]:
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
            "max_uses": 10,
        })

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
                "Analiza esta jornada. Busca bajas, alineaciones probables, "
                "el tiempo que hará en cada sede y cualquier circunstancia "
                "que mueva un partido y no esté en los números.\n\n"
                "Recuerda: si no encuentras nada concreto sobre una casilla, "
                "déjala como está."
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
        "coste_usd": round(
            mensaje.usage.input_tokens / 1e6 * PRECIO_ENTRADA
            + mensaje.usage.output_tokens / 1e6 * PRECIO_SALIDA,
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
                "max_uses": 5}],
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

    uso = {
        "tokens_entrada": mensaje.usage.input_tokens,
        "tokens_salida": mensaje.usage.output_tokens,
        "coste_usd": round(
            mensaje.usage.input_tokens / 1e6 * PRECIO_ENTRADA
            + mensaje.usage.output_tokens / 1e6 * PRECIO_SALIDA, 5),
        "busquedas_web": sum(
            1 for b in mensaje.content if b.type == "server_tool_use"),
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

    orden = {"alta": "media", "media": "baja", "baja": "baja"}
    sobreviven = []
    for a in ajustes.get("ajustes") or []:
        pos = int(a.get("posicion", 0))
        veredicto = (v.get(pos) or {}).get("veredicto", "mantener")
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

    contexto = formatear(datos, movimiento)
    print(f"  contexto: {len(contexto)} caracteres\n")

    print(f"Consultando a {MODELO}" + (" con búsqueda web" if not args.sin_web else "") + "...")
    texto, ajustes, uso = analizar(contexto, con_web=not args.sin_web)

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
        "senales": json.dumps(
            ajustes.get("senales") or [], ensure_ascii=False
        ) if isinstance(ajustes, dict) else None,
        **uso,
    })
    print(f"\nGuardado: análisis {r.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
