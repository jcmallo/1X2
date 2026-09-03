from pathlib import Path
import importlib.util
import sys

HERE = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location(
    "cf",
    HERE / "competiciones_femeninas.py",
)
cf = importlib.util.module_from_spec(spec)
sys.modules["cf"] = cf
spec.loader.exec_module(cf)


equipos = [
    cf.Equipo(1, "FC Barcelona Femení", "1132"),
    cf.Equipo(2, "Real Madrid CF Femenino", "6551"),
    cf.Equipo(3, "Atlético de Madrid Femenino", "1133"),
    cf.Equipo(4, "Real Sociedad Femenino", "1135"),
    cf.Equipo(5, "FC Badalona Women", "9999"),
]
por_id, por_nombre = cf.indice_equipos(equipos)


def html(rows, heading="quarter-finals"):
    return f"""
    <html><body>
      <h2>{heading}</h2>
      <table>
        {rows}
      </table>
    </body></html>
    """


# Copa: fecha/hora, rival externo, prórroga.
copa = html("""
<tr>
<td>05.02.2026 - 19:00 Uhr</td>
<td><a href="/en/real-sociedad/startseite/verein_1135.html">Real Sociedad</a></td>
<td>0:1 n.V.</td>
<td><a href="/en/badalona/startseite/verein_9999.html">FC Badalona Women</a></td>
</tr>
""")
ps, total = cf.parse_competicion_html(
    copa,
    temporada="2025-26",
    competicion="Copa de la Reina",
    url="https://example/copa",
    por_id=por_id,
    por_nombre=por_nombre,
)
assert total == 1 and len(ps) == 1
assert ps[0].hubo_prorroga is True
assert ps[0].equipo_local_id == 4
assert ps[0].equipo_visitante_id == 5


# Supercopa: exactamente las tres filas estructurales.
supercopa = html("""
<tr>
<td>20.01.2026 - 19:00 Uhr</td>
<td><a href="/en/rm/startseite/verein_6551.html">Real Madrid CF</a></td>
<td>3:1</td>
<td><a href="/en/atm/startseite/verein_1133.html">Atlético de Madrid</a></td>
</tr>
<tr>
<td>21.01.2026 - 19:00 Uhr</td>
<td><a href="/en/fcb/startseite/verein_1132.html">FC Barcelona</a></td>
<td>3:1</td>
<td><a href="/en/ath/startseite/verein_6210.html">Athletic Club</a></td>
</tr>
<tr>
<td>24.01.2026 - 19:00 Uhr</td>
<td><a href="/en/fcb/startseite/verein_1132.html">FC Barcelona</a></td>
<td>2:0</td>
<td><a href="/en/rm/startseite/verein_6551.html">Real Madrid CF</a></td>
</tr>
""", "Supercopa Femenina")
ps, total = cf.parse_competicion_html(
    supercopa,
    temporada="2025-26",
    competicion="Supercopa Femenina",
    url="https://example/super",
    por_id=por_id,
    por_nombre=por_nombre,
)
assert total == 3 and len(ps) == 3


# UWCL 2024-25: fila real conocida Manchester City - Barcelona 2:0.
uwcl = html("""
<tr>
<td>09.10.2024 - 21:00 Uhr</td>
<td><a href="/en/city/startseite/verein_999.html">Manchester City</a></td>
<td>2:0</td>
<td><a href="/en/fcb/startseite/verein_1132.html">FC Barcelona</a></td>
</tr>
<tr>
<td>08.10.2024 - 21:00 Uhr</td>
<td><a href="/en/chelsea/startseite/verein_888.html">Chelsea FC</a></td>
<td>3:2</td>
<td><a href="/en/rm/startseite/verein_6551.html">Real Madrid</a></td>
</tr>
""", "group stage")
ps, total = cf.parse_competicion_html(
    uwcl,
    temporada="2024-25",
    competicion="UEFA Women's Champions League",
    url="https://example/uwcl",
    por_id=por_id,
    por_nombre=por_nombre,
)
assert total == 2 and len(ps) == 2
assert ps[0].equipo_visitante_id == 1
assert ps[1].equipo_visitante_id == 2


# Current: future -:- -> PROGRAMADO.
future = html("""
<tr>
<td>22.09.2026 - 21:00 Uhr</td>
<td><a href="/en/fcb/startseite/verein_1132.html">FC Barcelona</a></td>
<td>-:-</td>
<td><a href="/en/arsenal/startseite/verein_777.html">Arsenal FC</a></td>
</tr>
""", "league phase")
ps, total = cf.parse_competicion_html(
    future,
    temporada="2026-27",
    competicion="UEFA Women's Champions League",
    url="https://example/current",
    por_id=por_id,
    por_nombre=por_nombre,
    actual=True,
)
assert total == 1 and len(ps) == 1
assert ps[0].estado == "PROGRAMADO"


# Alias typo histórico: Real Socieadad.
assert cf.resolver_equipo(
    "Real Socieadad", None, por_id, por_nombre
).equipo_id == 4

# Fallback UWCL actual: 4 partidos españoles.
fb = cf.fallbacks_uwcl_2026_27(por_id, por_nombre)
assert len(fb) == 4
assert all(p.estado == "FINALIZADO" for p in fb)

# ID estable no cambia al modificar hora.
p = fb[0]
a = cf.id_estable(p, "2026-27")
p.fecha_sql = p.fecha_sql[:11] + "20:30:00"
b = cf.id_estable(p, "2026-27")
assert a == b

print("ALL TESTS OK")
