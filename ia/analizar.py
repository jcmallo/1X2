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

# Precios por millón de tokens, para poder registrar lo que cuesta cada
# ejecución en lugar de estimarlo.
PRECIO_ENTRADA = 5.00
PRECIO_SALIDA = 25.00


SISTEMA = """Analizas jornadas de La Quiniela española para un sistema que ya
hace sus propios cálculos. Tu papel no es recalcular probabilidades: eso lo
hace un modelo estadístico entrenado con 4.400 partidos, y lo hace mejor que
tú. Tu papel es aportar lo que ese modelo no puede ver.

QUÉ TIENES DELANTE

Por cada casilla del boleto recibes cuatro cosas:

  % LAE        cuánta gente juega cada signo. Dice qué hace el público, no
               qué va a pasar.
  % Apuestas   probabilidad implícita en las cuotas, ya sin margen. Es el
               mejor estimador público que existe: hay dinero detrás.
  % Predictor  el modelo propio. Combina Elo, forma de las últimas cinco
               jornadas, goles recientes, descanso y enfrentamientos directos.
  Valor        Apuestas ÷ LAE. Por encima de 1 el signo está infrajugado y
               paga más de lo que le corresponde, porque el premio se reparte
               entre acertantes.

QUÉ SE ESPERA DE TI

Buscar en la prensa y razonar sobre lo que ninguno de esos números contiene:

  - Bajas, sanciones y lesiones de última hora, y sobre todo SI SON
    TITULARES. Que falte el tercer portero no cambia nada; que falte el
    delantero que mete la mitad de los goles, sí.
  - Alineaciones probables y rotaciones. Un equipo con Champions o Europa
    League entre semana no sale igual el domingo, y la prensa suele
    adelantarlo el día antes.
  - El clima de cada sede y a quién favorece. Un equipo de juego combinativo
    y suelo seco jugando en el norte con lluvia y viento no es el mismo
    equipo; un campo pesado iguala mucho.
  - Situación deportiva y ambiente: pelea por el descenso o el ascenso, un
    entrenador a punto de caer, un equipo ya clasificado que no se juega
    nada, crisis de vestuario, cambio de entrenador reciente.
  - Cualquier discrepancia grande entre las fuentes que tenga explicación
    concreta. Si el mercado da un 45% donde el público da un 61%, casi
    siempre hay una razón y a veces está publicada.

CÓMO BUSCAR

Busca por lo que necesitas saber, no por dónde crees que estará. Una consulta
como "Getafe Celta lesionados alineación probable" encuentra mejores fuentes
que ir a un periódico concreto: los sitios de fantasy y estadística suelen
tener alineaciones y partes médicos más completos y actualizados que la
prensa generalista, porque es su producto.

Incluye siempre la fecha o la jornada en la consulta. Sin eso salen noticias
de temporadas pasadas, y una lesión de hace meses probablemente esté resuelta
y en cualquier caso ya incorporada al precio de las cuotas.

Para el tiempo, busca la previsión de la ciudad A LA HORA DEL PARTIDO. Un
partido a las 21:00 en septiembre no tiene el tiempo del mediodía, y esa
diferencia es justo la que importa.

Liga F tiene mucha menos cobertura que las masculinas. Si buscas y no
encuentras nada de un partido femenino, dilo: es información en sí misma, y
mejor que rellenar con suposiciones.

REGLAS QUE NO PUEDES SALTARTE

1. NO cambies una casilla sin una razón concreta y verificable. "Intuición",
   "sensaciones" o "el Barcelona suele ganar" no son razones. Si no
   encuentras nada, deja la casilla como está: eso es una respuesta válida y
   frecuente. Una jornada con cero ajustes es un buen resultado.

2. El mercado suele tener razón. Cuando la contradigas, di exactamente qué
   sabes tú que el mercado no haya podido incorporar ya. Las cuotas se mueven
   con las noticias, así que una lesión publicada hace tres días ya está en
   el precio.

3. Prioriza el VALOR sobre la probabilidad. En la quiniela el premio se
   reparte entre acertantes: acertar lo que acierta todo el mundo paga poco.
   Un signo con valor 1,40 que falla a menudo puede ser mejor apuesta que un
   favorito con valor 0,80.

4. No inventes datos. Si buscas y no encuentras nada sobre un partido, dilo.
   Es mucho mejor que rellenar con suposiciones que suenan bien.

5. Sé conciso. Nadie va a leer tres páginas antes de sellar un boleto.

CÓMO RESPONDER

Un análisis breve (máximo 400 palabras) con lo que has encontrado que de
verdad importa, y después un bloque JSON exactamente con esta forma:

```json
{
  "ajustes": [
    {"posicion": 3, "signo_antes": "1", "signo_despues": "X",
     "confianza": "alta|media|baja",
     "razon": "qué sabes y por qué cambia la casilla"}
  ],
  "pleno": {"local": "1", "visitante": "0", "razon": "..."},
  "aviso": "lo que el sistema debería saber y no aparece en los ajustes"
}
```

Si no hay nada que ajustar, devuelve "ajustes": [] sin más."""


def reunir_contexto(api: ApiIngesta, temporada: str, jornada: int) -> dict:
    """Todo lo que el sistema sabe de la jornada, en un solo objeto."""
    return api.contexto_dashboard(
        temporada=temporada or None,
        numero_jornada=jornada or None,
    )


def formatear(datos: dict) -> str:
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

        lineas.append(
            f"{pos:>2}  {(c['local'][:18] + ' - ' + c['visitante'][:18]):<40} "
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


def main() -> int:
    p = argparse.ArgumentParser(description="Analiza la jornada con Claude.")
    p.add_argument("--temporada", default="")
    p.add_argument("--jornada", type=int, default=0)
    p.add_argument("--tipo", default="T-24", help="franja temporal")
    p.add_argument(
        "--sin-web", action="store_true",
        help="no buscar en internet: más barato, pero sin bajas ni clima",
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
    datos = reunir_contexto(api, args.temporada, args.jornada)
    if not datos.get("jornada"):
        print("No hay ninguna jornada cargada.")
        return 1

    j = datos["jornada"]
    print(f"Jornada {j['numero']} · {j['temporada']}")

    contexto = formatear(datos)
    print(f"  contexto: {len(contexto)} caracteres\n")

    print(f"Consultando a {MODELO}" + (" con búsqueda web" if not args.sin_web else "") + "...")
    texto, ajustes, uso = analizar(contexto, con_web=not args.sin_web)

    print(
        f"  {uso['tokens_entrada']:,} tokens de entrada, "
        f"{uso['tokens_salida']:,} de salida, "
        f"{uso['busquedas_web']} búsquedas · ${uso['coste_usd']:.4f}\n"
    )
    print(texto)

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
        **uso,
    })
    print(f"\nGuardado: análisis {r.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
