from pathlib import Path
import importlib.util
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "cm",
    HERE / "competiciones_masculinas.py",
)
cm = importlib.util.module_from_spec(spec)
sys.modules["cm"] = cm
spec.loader.exec_module(cm)

html = """
<html><body>
<div class="content-box-headline">Copa del Rey</div>
<table class="items">
<tr><th>Matchday</th><th>Date</th><th>Time</th><th>Venue</th>
<th>Opponent</th><th>Result</th></tr>
<tr>
<td>Round of 32</td><td>Wed 17/12/2025</td><td>9:00 PM</td><td>A</td>
<td><a href="/cf-talavera/startseite/verein/12345">CF Talavera</a></td>
<td><a href="/x/spielbericht/index/spielbericht/999999">2:3</a></td>
</tr>
</table>
<div class="content-box-headline">UEFA Champions League</div>
<table class="items">
<tr><th>Matchday</th><th>Date</th><th>Time</th><th>Venue</th>
<th>Opponent</th><th>Result</th></tr>
<tr>
<td>Group Stage</td><td>Wed 10/12/25</td><td>9:00 PM</td><td>H</td>
<td><a href="/man-city/startseite/verein/281">Man City</a></td>
<td><a href="/x/spielbericht/index/spielbericht/888888">1:2</a></td>
</tr>
</table>
</body></html>
"""

eq = cm.Equipo(
    equipo_id=1,
    nombre="Real Madrid",
    transfermarkt_id="418",
    transfermarkt_slug="real-madrid",
)

rows = cm.parse_calendario(
    html,
    equipo=eq,
    temporada="2025-26",
    calendario_url="https://example.test",
    seleccion={"Copa del Rey", "UEFA Champions League"},
)

assert len(rows) == 2, rows

copa = rows[0]
assert copa.competicion == "Copa del Rey"
assert copa.local_nombre == "CF Talavera"
assert copa.visitante_nombre == "Real Madrid"
assert copa.goles_local == 2
assert copa.goles_visitante == 3
assert copa.id_fuente == "999999"

ucl = rows[1]
assert ucl.competicion == "UEFA Champions League"
assert ucl.local_nombre == "Real Madrid"
assert ucl.visitante_nombre == "Man City"
assert ucl.fecha_sql == "2025-12-10 21:00:00"

print("OK parser Transfermarkt sintético")
