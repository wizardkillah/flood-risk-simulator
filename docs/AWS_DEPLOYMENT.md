# Despliegue en AWS — libre acceso público

Sí es posible poner el simulador en AWS con acceso público gratuito para el
usuario final (paga el operador de la cuenta AWS + Anthropic). Esta guía
propone tres arquitecturas de menor a mayor complejidad y costo, y detalla
paso a paso la recomendada.

> **Realidad de "gratis"**: para el visitante siempre es gratis. Para
> vos como operador hay costo AWS + costo Anthropic (cada análisis IA y
> cada mensaje del chatbot es una llamada pagada). Con el free tier del
> primer año, tráfico modesto (~1.000 sesiones/mes) y uso moderado del
> chat, el total ronda **USD 15-40/mes**. Ver desglose abajo.

---

## Perfil de la aplicación

Antes de decidir arquitectura conviene entender qué se está desplegando:

| Componente | Tamaño | Naturaleza |
|-----------|--------|-----------|
| Frontend estático (HTML/CSS/JS) | ~50 KB | CDN clásico |
| Teselas de terreno (`data/terrain_tiles/`) | ~300 MB, 5.316 archivos | Estático, cacheable a muerte |
| DEM crudo (`data/dem/limari_cop30.tif`) | 325 MB | Solo lo necesita el backend para `terrain_profile` |
| Datos hídricos (`data/hydro/*.geojson`) | ~30 MB | Servido por el backend |
| Backend FastAPI | ~200 MB de dependencias (rasterio, geopandas) | Procesos Python |
| Endpoints "caros" | `/api/flood?rainfall_mm`, `/api/terrain_profile`, `/api/ai_analysis`, `/api/chat` | CPU y llamadas a Anthropic |

La mayor parte del tráfico son las **teselas de terreno + imagería
satelital**. Ese tráfico debe salir por CDN o cuesta caro.

---

## Arquitectura recomendada — S3 + CloudFront + App Runner

```
                    ┌────────────────────────────┐
   Usuario ────►    │   CloudFront (CDN global)  │
                    └────────────┬───────────────┘
                                 │
              ┌──────────────────┴────────────────┐
              │                                   │
              ▼                                   ▼
      ┌───────────────┐                  ┌───────────────────┐
      │  S3 bucket    │                  │  App Runner       │
      │  - frontend/  │                  │  FastAPI (Docker) │
      │  - terrain/   │                  │  512MB / 0.25vCPU │
      │  (público RO) │                  │  auto-scale 0..N  │
      └───────────────┘                  └────────┬──────────┘
                                                  │
                                                  ▼
                                         ┌──────────────────┐
                                         │ Secrets Manager  │
                                         │ ANTHROPIC_API_KEY│
                                         └──────────────────┘
```

**Por qué esta partición:**
- Todo lo estático (frontend + teselas de terreno) va a S3 y se sirve por
  CloudFront con caché de 1 año. Es lo que domina el tráfico y así casi no
  toca al backend.
- El backend en App Runner solo atiende `/api/*`. Escala a 0 cuando no hay
  tráfico (ahorra cuando el sitio está inactivo).
- CloudFront enruta por path (`/terrain/*` y `/` → S3, `/api/*` → App
  Runner) para que el usuario vea una sola URL.
- La API key vive en Secrets Manager, no en la imagen ni en variables
  planas del servicio.

### Alternativas más simples (y por qué las descarto)

| Alternativa | Cuándo tiene sentido | Por qué NO acá |
|-------------|---------------------|----------------|
| **EC2 t3.small único** con nginx | Prototipo rápido, un solo servidor | Cuesta ~USD 15/mes fijo aunque no haya tráfico, y sirve las teselas desde EC2 (caro y lento globalmente) |
| **Lambda + API Gateway** | Backend liviano y stateless | rasterio + geopandas + numpy pesan >200 MB descomprimidos: pasa el límite de Lambda sin container images. Container Lambda funciona pero cold start es lento para HAND. |
| **Elastic Beanstalk** | Legacy, si ya conocés | Menos moderno que App Runner, más YAML y más costo base |
| **ECS Fargate directo** | Tráfico alto sostenido | Más configuración (VPC, ALB, target groups) sin ventaja para este volumen |

---

## Paso a paso

Asumimos AWS CLI configurada y una región (ej: `us-east-1` — CloudFront
requiere us-east-1 para certificados ACM).

### 0) Preparación

```bash
export AWS_REGION=us-east-1
export APP_NAME=simulador-crecidas
export BUCKET=$APP_NAME-static-$(date +%s)
```

### 1) Bucket S3 con frontend + teselas

```bash
aws s3 mb "s3://$BUCKET" --region $AWS_REGION
aws s3 sync frontend/ "s3://$BUCKET/" \
  --exclude "*.py" --cache-control "public,max-age=300"
aws s3 sync data/terrain_tiles/ "s3://$BUCKET/terrain/" \
  --cache-control "public,max-age=31536000,immutable" \
  --content-encoding gzip \
  --exclude "*" --include "*.terrain"
aws s3 cp data/terrain_tiles/layer.json "s3://$BUCKET/terrain/layer.json" \
  --content-type application/json
```

Bloquear escritura pública, dejar solo lectura pública de objetos
(CloudFront hace el gate).

### 2) Imagen Docker del backend

Crear `Dockerfile` en la raíz:

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin libgdal-dev gcc && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY backend/ backend/
COPY data/hydro/ data/hydro/
COPY data/flood/ data/flood/
COPY data/dem/ data/dem/
COPY data/markers.json data/markers.json
ENV PORT=8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

Nota: NO copiar `data/terrain_tiles/` a la imagen (300MB de teselas van a
S3, no al backend).

### 3) Push a ECR + App Runner

```bash
aws ecr create-repository --repository-name $APP_NAME
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password | docker login --username AWS \
  --password-stdin $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
docker build -t $APP_NAME .
docker tag $APP_NAME:latest $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$APP_NAME:latest
docker push $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$APP_NAME:latest
```

En consola de App Runner → *Create service* → *Container registry* → ECR
→ elegir la imagen. Configuración recomendada:
- CPU: 0.25 vCPU
- Memoria: 512 MB (o 1 GB si HAND te queda ajustado)
- Concurrency: 20 requests
- Auto scaling: min 1, max 3 (o min 0 si tolerás cold starts de ~10s)
- Health check: HTTP `/api/flood/config`

### 4) Secret con la API key

```bash
aws secretsmanager create-secret \
  --name $APP_NAME/anthropic \
  --secret-string '{"ANTHROPIC_API_KEY":"sk-ant-..."}'
```

Dar permiso al role de App Runner (`AppRunnerECRAccessRole` +
`SecretsManagerReadWrite` acotado a este secret) y referenciarlo en la
config del servicio como env var.

### 5) CloudFront delante de S3 + App Runner

Crear una distribución con dos *origins*:
- **S3 origin** (con OAC — Origin Access Control) para el bucket
- **App Runner origin** (custom) apuntando al URL `xxx.awsapprunner.com`

Y dos *behaviors*:
- Path pattern `/api/*` → App Runner, sin caché
- Default `*` → S3, caché 1 año para `.terrain`, 5 min para HTML/JS

Al terminar, la URL pública de CloudFront es lo que compartís.

### 6) Dominio propio (opcional)

- Registrar en Route 53 o traer desde otro registrar
- Certificado ACM en us-east-1
- Alias A/AAAA → CloudFront

---

## Costo estimado mensual

Escenario: 1.000 sesiones/mes, promedio 100 teselas descargadas por sesión
(300MB caché caliente, ~10MB por sesión nueva), 50 mensajes al chatbot y
20 análisis IA por mes.

| Servicio | Detalle | USD/mes |
|----------|---------|---------|
| S3 Standard | 350MB almacenados + PUTs iniciales | ~0.10 |
| CloudFront | ~10 GB salida + 200k requests | ~1.20 |
| App Runner | 0.25 vCPU × 24h + 512MB × 24h (con min=1) | ~7-10 |
| Secrets Manager | 1 secret | 0.40 |
| Route 53 (si aplica) | 1 zona | 0.50 |
| Data transfer S3→CloudFront | (interno, gratis) | 0 |
| **Anthropic API** (Opus 4.8) | ~20 análisis IA + 50 chats | **~8-20** |
| **Total** | | **~15-35** |

Con `min instance = 0` en App Runner cae a ~USD 3-5 fijos si el sitio está
inactivo, pero cada primera visita paga ~10s de cold start.

**Free tier del primer año** cubre casi todo lo estático (5GB S3, 1M
CloudFront requests, 100GB salida) — realmente lo que cuesta es App
Runner + Anthropic API.

---

## Cómo abaratar / limitar exposición

- **Cache-control agresivo** en las teselas (ya está: `max-age=31536000`)
  — 99% de hits al edge, casi nada al origen.
- **Rate limit** en el chatbot y en `/api/ai_analysis` (ya implementados)
  para acotar el gasto Anthropic aunque alguien haga un bucle.
- **AWS Budgets** con alerta al 50/80/100% de USD 30 para no llevarte
  sorpresas.
- **CloudFront WAF** con regla básica anti-bot si el sitio se hace
  público-público (rate limit por IP a nivel edge). Cuesta ~USD 5/mes.
- **Suspender min-instance** en horario nocturno con EventBridge si
  querés bajar de USD 10 fijos.

---

## Qué NO hacer

- Servir las 5.316 teselas de terreno desde EC2 o desde App Runner
  directo: matás el costo de tráfico y la latencia global.
- Meter la API key en el frontend o en variables planas del contenedor.
- Dejar el bucket S3 públicamente listable — solo lectura de objetos, y
  mejor con OAC vía CloudFront.
- Correr el pipeline de datos (scripts 01..10) dentro de App Runner. Ese
  pipeline se corre local o en una instancia EC2 puntual cuando toca
  regenerar los datos, después se suben a S3.

---

## Checklist antes de exponer

- [ ] `.env` NO está en el repo ni en la imagen Docker (`.dockerignore`)
- [ ] `ANTHROPIC_API_KEY` viene solo de Secrets Manager
- [ ] Banner del prototipo visible ("NO usar para decisiones de
  evacuación")
- [ ] Rate limit del chat y del asesor IA verificados con `hey` o `ab`
- [ ] Presupuesto AWS con alerta configurada
- [ ] CORS del backend cerrado a tu dominio (no `*`) si vas a producción
- [ ] `AWS_ADVISOR_MODEL` / `CHAT_MODEL` fijados al modelo que decidiste
  (Opus 4.8 razona mejor, Sonnet 5 es más barato — probar cuál te alcanza)
