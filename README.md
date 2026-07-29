# ZK/FRG Gastos Bot — Guía monkey-proof

Bot de Telegram que recibe fotos de estados de cuenta, los clasifica
(ZK Operativo vs FRG Personal) con Claude, y los escribe en el Google Sheet en vivo.

Sheet en vivo: https://docs.google.com/spreadsheets/d/1sJJhnz9tNqNkTuDG5g9QIaqybC39Ew6MF_7Z2MEb4Og/edit

---

## Lo que necesitas antes de desplegar (checklist)

### 1. Token de Telegram
Ya lo tienes de BotFather. Si lo revocaste por seguridad, genera uno nuevo con `/token` en el chat de @BotFather.

**Por qué:** es la llave que conecta tu bot con los servidores de Telegram.

### 2. API Key de Anthropic (Claude)
1. Ve a https://console.anthropic.com
2. Inicia sesión con tu cuenta (la misma de Claude Pro o crea una de API — son cuentas separadas)
3. Ve a "API Keys" → "Create Key"
4. Copia la key (empieza con `sk-ant-...`)

**Por qué:** el bot usa la API de Claude (no tu Claude.ai) para leer y clasificar las imágenes. Se paga por uso — muy barato para este volumen (centavos de dólar por estado de cuenta).

### 3. Cuenta de servicio de Google (para escribir al Sheet)
Esto es lo único "técnico" de verdad. Pasos:

1. Ve a https://console.cloud.google.com
2. Crea un proyecto nuevo (arriba a la izquierda, "Nuevo proyecto") — nómbralo `zk-frg-bot`
3. En el buscador de arriba escribe "Google Sheets API" → Habilitar
4. Ve a "APIs y servicios" → "Credenciales" → "Crear credenciales" → "Cuenta de servicio"
5. Dale cualquier nombre (ej. `bot-gastos`) → Crear y continuar → Listo
6. Click en la cuenta de servicio que se creó → pestaña "Claves" → "Agregar clave" → "Crear clave nueva" → tipo **JSON** → Crear
7. Se descarga un archivo `.json` — **ese es tu `GOOGLE_SERVICE_ACCOUNT_JSON`**, ábrelo con el Bloc de notas, copia TODO el contenido
8. Dentro de ese JSON busca el campo `"client_email"` — copia ese correo (algo como `bot-gastos@zk-frg-bot.iam.gserviceaccount.com`)
9. Abre el Sheet (link arriba) → botón "Compartir" → pega ese correo → dale permiso de **Editor**

**Por qué:** el bot no puede "iniciar sesión como tú" en Google. Necesita su propia identidad (la cuenta de servicio) con permiso explícito sobre TU Sheet.

### 4. Cuenta en Railway
1. Ve a https://railway.app → Login con GitHub
2. Si no tienes GitHub, créate una cuenta gratis en https://github.com primero

---

## Desplegar el bot (una vez tengas lo anterior)

1. Sube esta carpeta (`main.py`, `requirements.txt`, `Procfile`) a un repo nuevo en GitHub
   - Más fácil: en GitHub → "New repository" → arrastra los 3 archivos directo en el navegador
2. En Railway → "New Project" → "Deploy from GitHub repo" → selecciona el repo
3. Railway detecta Python solo. Ve a la pestaña **Variables** del proyecto y agrega:

   | Variable | Valor |
   |---|---|
   | `TELEGRAM_TOKEN` | el token de BotFather |
   | `ANTHROPIC_API_KEY` | tu key de console.anthropic.com |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | pega TODO el contenido del archivo .json (una sola línea está bien) |
   | `SHEET_ID` | `1sJJhnz9tNqNkTuDG5g9QIaqybC39Ew6MF_7Z2MEb4Og` |
   | `ALLOWED_USER_IDS` | (déjalo vacío por ahora) |

4. Railway redepliega solo al guardar variables. En "Deployments" verifica que diga "Success" y en los logs veas `Bot arrancando (polling)...`

## Probarlo

1. Abre tu bot en Telegram: `t.me/gastos_frzk_bot`
2. Manda `/start` — te va a responder con tu `chat_id`
3. Copia ese número y ponlo en la variable `ALLOWED_USER_IDS` en Railway (así nadie más puede usar tu bot)
4. Manda una foto de un estado de cuenta → espera la clasificación → revisa el Sheet

## Costos aproximados
- Railway: gratis hasta cierto uso, luego ~$5 USD/mes (proceso corriendo 24/7)
- Anthropic API: pago por uso, estimado <$1 USD/mes para este volumen
- Telegram: gratis siempre
