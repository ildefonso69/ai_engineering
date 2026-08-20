# CI construido por etapas — material del directo (Sesión 15)

Estos ficheros **no se ejecutan**. Viven fuera de `.github/workflows/` a propósito:
GitHub Actions solo ejecuta lo que hay en `.github/workflows/`, así que aquí se
pueden guardar versiones intermedias sin disparar builds.

Son las **cuatro etapas** por las que pasa el pipeline en el directo. La idea es
que nadie vea un `ci.yml` de 300 líneas caído del cielo: se empieza por un job
que instala y testea, y se le van añadiendo capas, viendo cada una en verde
antes de añadir la siguiente.

| Paso | Fichero | Qué añade | Pregunta que responde |
|---|---|---|---|
| 1 | `step-1-test.yml` | Un job: instalar + testear el servicio IA con el LLM mockeado | ¿El código funciona? |
| 2 | `step-2-both-projects.yml` | Segundo job para Rails + filtros de path | ¿Y el otro proyecto, sin reconstruirlo todo? |
| 3 | `step-3-contract.yml` | Contract test entre capas | ¿Siguen entendiéndose las dos capas? |
| 4 | `step-4-build.yml` | Build y publicación de ambas imágenes en GHCR | ¿Esto se puede empaquetar y enviar? |
| 5 | `step-5-deploy.yml` | Despliegue por SSH a la EC2 + smoke test | ¿Y llega a la máquina de verdad? |

El resultado final —el mismo que el paso 5, más los ganchos comentados de la Sesión 16— es
`.github/workflows/ci.yml`. Los pasos 4 y 5 se **extraen del `ci.yml` real**, así que no pueden
divergir de él.

**Cómo usarlos en vivo:**

```bash
# Etapa 1: copiar y empujar
cp .github/ci-steps/step-1-test.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml && git commit -m "ci: test the AI service with the LLM mocked"
git push        # mirar Actions

# ... repetir con step-2, step-3, step-4, step-5
```

> Cada fichero es **completo y ejecutable por sí mismo**, no un fragmento. Así se
> puede empujar cualquiera de ellos y ver el pipeline entero en ese estado.
