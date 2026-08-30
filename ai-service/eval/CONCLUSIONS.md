# Conclusiones: Análisis de Configuraciones de Búsqueda

## Trade-offs por Configuración

### Config A: Vector Search (sin reranking)
- **Velocidad**: ⚡⚡⚡ Más rápida (baseline)
- **Relevancia**: ⭐⭐ Solo embedding similarity
- **Costo**: 🟢 Mínimo (solo embedding)
- **Mejor para**: Latencia crítica, corpus homogéneo

### Config B: Hybrid RRF (sin reranking)
- **Velocidad**: ⚡⚡ +20-50% latencia vs A
- **Relevancia**: ⭐⭐⭐ Vector + keywords, ambas perspectivas
- **Costo**: 🟡 Embedding + FTS (ambos rápidos)
- **Mejor para**: Recall amplio, balance velocidad-calidad

### Config C: Vector + Cross-encoder (reranking)
- **Velocidad**: ⚡ Lento (~50-100ms reranking)
- **Relevancia**: ⭐⭐⭐⭐ Refinamiento fino de vector
- **Costo**: 🟠 Embedding + NN inference (moderado)
- **Mejor para**: High precision, latencia tolerada

### Config D: Hybrid RRF + Cross-encoder (reranking)
- **Velocidad**: 🐢 Más lento (ambas búsquedas + reranking)
- **Relevancia**: ⭐⭐⭐⭐⭐ Máxima (todos los signals)
- **Costo**: 🔴 Máximo
- **Mejor para**: Máxima calidad, latencia no crítica

---

## Matriz de Decisión

| Caso de Uso | Config Recomendada | Justificación |
|---|---|---|
| **Búsqueda interactiva (UI)** | **B** (Hybrid) | Recall amplio sin penalidad latencia crítica. RRF combina vector+keywords de forma rápida. |
| **APIs batch / offline** | **D** (Hybrid + Rerank) | Máxima precisión aceptable porque latencia no es crítica. Reranking justificado. |
| **Mobile / edge** | **A** (Vector) | Mínima latencia, no hay GPU/CPU para reranking. Embedding es lo más que se puede hacer. |
| **Enterprise search** | **C o D** (según SLA) | Si P95 latency < 500ms: Config C. Si < 2s: Config D. Relevancia prima sobre velocidad. |
| **Búsqueda sobre corpus muy grande** | **B** (Hybrid) | RRF es el mejor equilibrio. Reranking en top-50 sigue siendo caro con millones de docs. |

---

## Justificación Técnica

### ¿Reranking Justificado?

**Sí, pero con condiciones:**

1. **Ganancias típicas** (basadas en literatura RAG):
   - Vector a Hybrid: +5-15% precisión (RRF cost barato)
   - Vector a Hybrid+Rerank: +8-25% precisión (costo moderado)
   - Hybrid+Rerank es máximo (~95% en casos controlados)

2. **Cuándo NO justificado:**
   - Latencia P95 < 200ms requerida → A o B sin rerank
   - Corpus < 10k docs → overhead reranking no vale la pena
   - Embeddings ya muy buenos (e.g., OpenAI text-embedding-3-large) → A suficiente

3. **Cuándo SÍ justificado:**
   - Latencia P95 > 500ms tolerable → Rerank compensa (gain: ~10-20%)
   - Precisión crítica (e.g., legal discovery, medical research) → D
   - Híbrida ya da recall amplio → Rerank cost marginal si ya hay top-50

---

## Recomendación Final para este Proyecto

### Para Estimación de Proyectos (Master AI Engineering)

**Recomendación: Config B (Hybrid RRF sin reranking)**

#### Justificación:

1. **Trade-off óptimo**:
   - Híbrida captura vector (semántica profunda) + keywords (términos técnicos: "cloud", "API", "machine learning")
   - RRF es matemáticamente sólido y rápido
   - No requiere modelo ML adicional en la ruta crítica

2. **Domain fit**:
   - Presupuestos contienen vocabulario específico (sectors, tecnologías)
   - Búsqueda de proyecto = buscar por descripción (vectorial) + términos clave (lexical)
   - Ambas perspectivas necesarias, una sola no basta

3. **Latencia tolerable**:
   - Estimación NO es interactive search en UI en tiempo real
   - P95 latency ~200-300ms (hybrid) < 1s tolerable
   - Reranking (Config C/D) agrega ~100-200ms más por poco gain (~5-10%)

4. **Simplicidad operacional**:
   - Sin dependencia de GPU/ONNX para cross-encoder
   - RRF stateless, no state a sincronizar
   - Mantenimiento mínimo (vs. tuning reranker)

#### Alternativa si datos confirman baja precisión:
- **Si P@5 < 0.6 con Config B** → Evaluar Config D (reranking)
- **Si latencia permite** y ganancia de precisión > 15% → Cambiar a D

---

## Cómo Usar Este Análisis

1. **Ejecutar `benchmark_golden_set.py`** con datos reales
2. **Registrar** P@5 y latencia en tabla de resultados
3. **Comparar** resultados reales vs. esperados en matriz arriba
4. **Decidir**:
   - Si precisión aceptable con B → usar B
   - Si ganancia D >> costo → escalar a D
   - Si latencia crítica → downgrade a A

---

## Contexto de Decisión

**Métricas a considerar** (en orden de importancia para este proyecto):

1. **Precisión@5** (60% weight): ¿Qué tan buenos son los top-5 presupuestos?
2. **Recall** (20% weight): ¿Se pierden presupuestos relevantes?
3. **Latencia P95** (15% weight): ¿Aceptable para experiencia usuario?
4. **Costo de infra** (5% weight): GPU/CPU reranking caro?

Scoring: **Config B + upgrade a D si Precisión baja después de medir.**

---

## Próximos Pasos

- [ ] Ejecutar benchmark con datos reales de corpus
- [ ] Anotar ground truth en 5 queries (2-3h)
- [ ] Medir P@5, latencia, recall para A, B, C, D
- [ ] Generar tabla final
- [ ] Decisión: ¿Mantener B o cambiar a D?
- [ ] Deploy config elegida, monitorear en producción
