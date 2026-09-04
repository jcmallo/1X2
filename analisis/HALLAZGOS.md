# Hallazgos sobre el histórico de La Quiniela

Datos: 989 jornadas entre 2009/10 y 2025/26 con probabilidad de mercado
(BetFair), proporción apostada (LAE), resultado real y premio unitario de
todas las categorías. 13.846 casillas.

Fuentes: `ValoracionesCoeficiente_BtF_LAE.xlsb` y `Estadistica Real.xlsb`
(Quinielandia). Reproducible con `python -m analisis.backtest --datos <carpeta>`.

---

## 1. El mercado está calibrado; el público no

| | Acierto de signo | Log-loss |
|---|---|---|
| Mercado (BetFair) | **52,69%** | **0,9702** |
| Público (LAE) | 51,82% | 1,0018 |

Cuando el mercado dice 35,4%, ocurre el 35,1% de las veces. El error medio
de calibración es de ~1 punto porcentual en todos los tramos.

El público se desvía de forma sistemática:

| Tramo (prob. mercado) | Mercado | Público | Real | Sesgo |
|---|---|---|---|---|
| 20-30% | 0,258 | 0,219 | 0,250 | **−3,1 pp** |
| 40-50% | 0,448 | 0,498 | 0,446 | +5,2 pp |
| 50-60% | 0,546 | 0,638 | 0,554 | +8,3 pp |
| **60-70%** | 0,645 | 0,746 | 0,636 | **+11,1 pp** |
| 70-80% | 0,745 | 0,823 | 0,784 | +3,8 pp |

Sobreapuesta los favoritos moderados e infraapuesta los outsiders del
20-30%. El sesgo es estable a lo largo de 17 temporadas.

**Consecuencia:** batir al mercado en predicción es la vía equivocada, está
calibrado al 1%. La oportunidad está en que el premio lo reparte el público.

---

## 2. Acertar y cobrar están anticorrelacionados

El hallazgo central. El premio de una categoría no es un dato fijo: depende
de cuánta gente acertó lo mismo.

| Popularidad de la columna jugada | Premio medio cobrado |
|---|---|
| Q1 — la menos jugada | **5,00 €** |
| Q2 | 3,08 € |
| Q3 | 2,16 € |
| Q4 — la más jugada | **1,10 €** |

Factor **4,5×** entre jugar donde no está el público y jugar donde está.

Traducido a estrategias, comparando lo cobrado con lo que paga esa categoría
en una jornada típica:

| Estrategia | Cobra | cat 10 | cat 11 |
|---|---|---|---|
| Favorito del mercado | **3%** | 30% | 12% |
| Value min 30% | **9%** | 53% | 28% |

Se acierta justo cuando sale lo previsible, y entonces han acertado miles de
personas más. El favorito acierta más signos y cobra un tercio.

---

## 3. El value no es una estrategia por sí solo

Elegir el signo con mejor ratio `mercado / público` sin restricciones lleva
a columnas con ratio 8,3 y probabilidad de una entre un millón: acierta el
27% de los signos frente al 53% del favorito.

Funciona **solo** con una probabilidad mínima. Es un desempate entre signos
plausibles, no un criterio de selección.

---

## 4. Ninguna estrategia de columna única resultó rentable

Backtest con premios reales, una columna por jornada, 989 jornadas:

| Estrategia | Acierto | ROI | IC 90% (bootstrap) | P(ROI>0) |
|---|---|---|---|---|
| Favorito mercado | 52,7% | −44,8% | [−58%, −30%] | 0% |
| Favorito público | 51,8% | −69,2% | [−75%, −62%] | 0% |
| Value min 25% | 44,2% | −22,9% | [−62%, +29%] | 21% |
| **Value min 30%** | 51,8% | **−14,4%** | [−40%, +17%] | 20% |
| Value min 35% | 52,8% | −35,0% | [−56%, −11%] | 1% |

Demostrado:

- Los favoritos son perdedores con certeza: su intervalo no roza el cero.
- El value mejora ~30 puntos sobre el favorito, y la mejora es
  significativa: la mediana del value (−16,5%) cae fuera del intervalo del
  favorito ([−58%, −30%]).
- Con el mismo acierto de signo que el público (51,8%), el value gana 2,8
  veces más (566 € frente a 204 €).

No demostrado: que exista un umbral rentable.

---

## 5. Las carteras reducen la varianza pero no aumentan el valor

Resultado inesperado y con consecuencias prácticas.

| Columnas | Coste | EV | ROI esperado |
|---|---|---|---|
| 1 | 661 | 1.521 | +130% |
| 16 | 10.580 | 1.228 | −88% |
| 256 | 169.280 | 1.218 | −99% |

El EV se mantiene plano mientras el coste se multiplica, porque **el fondo de
cada categoría es fijo**. Si las columnas propias acaparan una categoría, se
cobra ese fondo repartido entre ellas mismas: más boletos ganadores no es más
dinero, es el mismo dinero dividido entre más participaciones propias.

Explica por qué el experimento de las 80.000 columnas de Quinielandia solo
era rentable en el 30% de las jornadas.

**Diversificar sirve para estabilizar el resultado, no para mejorarlo.**

---

## 6. El valor esperado está en sucesos que casi nunca pasan

Desglose del EV de una columna (value min 30%):

| Categoría | EV aportada | % del total | Veces esperadas en 17 años |
|---|---|---|---|
| 10 | 142 € | 9% | 69,7 |
| 11 | 299 € | 20% | 29,6 |
| 12 | 639 € | 42% | 9,2 |
| 13 | 263 € | 17% | **1,9** |
| 14 | 178 € | 12% | **0,18** |

El 71% del valor está en categorías que ocurren entre 9 y 0,2 veces en 17
años. Por eso el EV da +130% y lo realizado fue −14,4%: se esperaban ~2
sucesos de categoría 13-14 y ocurrió 1.

Con 989 jornadas **no se puede distinguir si el EV es correcto o si el modelo
sobreestima**. Un EV positivo concentrado en sucesos rarísimos no es una
estrategia practicable sin capital para aguantar la varianza.

---

## 7. El modelo de reparto sí está validado

Estimando los acertantes como `apuestas_validadas × P_LAE(acertar k)` y
contrastando con el escrutinio real:

| Categoría | n | estimado / real |
|---|---|---|
| 10 | 989 | 0,985 |
| 11 | 989 | 1,020 |
| 12 | 989 | 1,071 |
| 13 | 989 | 1,136 |
| 14 | 989 | 1,076 |

Error del 1-14%. La parte del cálculo que predice cuánta gente acierta
funciona; la incertidumbre está en la frecuencia de los sucesos raros, no en
el reparto.

---

## Errores metodológicos detectados durante el análisis

Los tres del mismo tipo: creerse un número sin comprobar de dónde salía.

**1. Barrido de umbral con "óptimo" en 27% (+15,8% ROI).**
El 53% de esa ganancia venía de una sola jornada (2017/18_08). Sin ella:
−45,4%. Delator: la oscilación entre umbrales contiguos (26% → −56%,
27% → +16%, 28% → −20%). Un punto de umbral no mueve el ROI 70 puntos.

**2. Carteras de 6 dobles con +356% ROI.**
El 85% venía de una jornada (2016-17_20). Sin ella: −30%.

**3. Esperanza matemática de +4886% para una sola columna.**
Multiplicaba P(acertar) por el premio medio como si fueran independientes.
No lo son (ver punto 2). Corregido estimando el premio a partir de la
proporción apostada.

---

## Reglas de trabajo que salen de aquí

1. **Todo ROI va con bootstrap.** Un número sin intervalo de confianza no es
   un resultado.
2. **Antes de creerse un óptimo, mirar de dónde sale el dinero.** Si un
   suceso aporta más del 20% del total, no hay estrategia que medir.
3. **Un barrido de parámetros sobre datos escasos encuentra ruido.** La
   oscilación entre valores contiguos lo delata.
4. **Nada que dependa del premio puede asumir independencia con el acierto.**
   En un sistema de apuesta mutua, el premio depende de quién más acertó.

---

## Qué queda por explorar

- **El bote.** Es lo único que rompe la restricción del fondo fijo: cuando
  hay arrastre, el fondo de la categoría 14 no procede solo de la
  recaudación de esa jornada. No se ha analizado.
- **El Pleno al 15**, que tiene reparto propio y no entra en este análisis.
- **Modelo propio de probabilidad.** Todo lo anterior usa la probabilidad de
  mercado. La pregunta abierta es si un modelo con datos deportivos, clima,
  plantillas y prensa aporta algo sobre un mercado ya calibrado al 1%.
