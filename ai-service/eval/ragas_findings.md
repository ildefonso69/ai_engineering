# Hallazgos de Evaluación RAGAS — Sesión 11 Groundedness

## Línea Base de Calidad

Se ejecutó RAGAS (Retrieval-Augmented Generation Assessment) sobre el golden set extendido con cinco consultas de referencia (ecommerce, healthcare, logistics, finance, multi-sector) usando las cuatro métricas estándar: **faithfulness**, **answer relevancy**, **context precision** y **context recall**.

## Anomalías Esperadas

### 1. **Faithfulness moderada en el multi-sector (case-005)**
La consulta case-005 ("training provider consolidating three products") es deliberadamente un edge case que abarca dos sectores dispares (educación y media). Aunque la estimación de referencia está desglosada de forma realista (192 engineer-days), es probable que `faithfulness` caiga a 0.65–0.75 en esta consulta porque:
- El corpus tiene historiales de plataformas de aprendizaje (~80 días) y plataformas de vídeo (~70 días) por separado, pero ninguno cubre la integración de AMBOS.
- El modelo debe componer evidencia de dos sectores sin un histórico único que lo respalde, lo que reduce la confianza de apoyo.

### 2. **Context Precision bajo si el retrieval falla en la segmentación**
Las consultas healthcare y finance incluyen requisitos de compliance (HIPAA, regulación financiera) que a menudo no viajan explícitamente en los históricos de presupuestos. Si el retriever no amplía suficientemente (ej., no busca "compliance" además de "healthcare"), context precision podría caer a 0.5–0.6 porque incluirá muchos chunks irrelevantes de otras consultas healthcare sin compliance.

### 3. **Context Recall bajo es normal y esperado**
Entre todas las consultas, context recall estará probablemente en el rango 0.4–0.6 porque:
- El corpus es finito (~60 proyectos, ~1.5k históricos) y es raro recuperar todos los antecedentes válidos.
- Caso de estudio: logistics (case-003) tiene solo 4 proyectos históricos en el corpus pero necesita analógos en route optimization, offline sync, y telematics. Es probable que `context_recall` sea ~0.5.

### 4. **Answer Relevancy alto en todos los casos**
Debería estar cerca de 0.9 porque las estimaciones en el golden set están estructuradas como respuestas directas a la pregunta ("cuántos engineer-days"). No hay anomalía esperada aquí.

## Acciones Post-Baseline

Una vez capturada esta línea base:
1. Ejecutar el pipeline real contra el corpus con el modelo actual y comparar con ground_truth.
2. Identificar qué consulta tiene la mayor brecha (probablemente case-005).
3. Usar `verify_citations()` para confirmar si las caídas en faithfulness correlacionan con citaciones colgantes (dangling citations).
4. Si context precision es baja, revisar si el router (Session 10 retrieval routing) está eligiendo la colección correcta o si necesita expansión de query.
