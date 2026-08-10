<p align="center">
  <img src="assets/logo.png" alt="axion-wizard" width="180">
</p>

# axion-wizard

Instalador/orquestador del stack AXION (Mattermost + WireGuard + Ollama +
FastAPI sobre Docker).

## Empezar

Un solo comando monta el entorno, instala las dependencias y arranca el
wizard. Es idempotente: se puede repetir sin romper nada.

**Windows (PowerShell)**

```powershell
.\scripts\bootstrap.ps1
```

**Linux / macOS / WSL**

```bash
./scripts/bootstrap.sh
```

El script busca un Python >= 3.11, crea `.venv` e instala todo. Usa
[uv](https://docs.astral.sh/uv/) si lo encuentra (respeta `uv.lock`, entorno
reproducible) y cae a `venv` + `pip` si no. No instala uv por su cuenta.

Si algo falla, el error dice qué pasó y qué hacer — no hay que interpretar un
stack trace.

### Pasarle argumentos al wizard

Lo que va detrás llega tal cual al wizard:

```powershell
.\scripts\bootstrap.ps1 doctor            # Windows
```

```bash
./scripts/bootstrap.sh -- doctor          # Linux/macOS
```

### Otras variantes

| Objetivo | Windows | Linux/macOS |
|---|---|---|
| Solo preparar el entorno | `.\scripts\bootstrap.ps1 -NoRun` | `./scripts/bootstrap.sh --no-run` |
| Preparar + lint/tipos/tests | `.\scripts\bootstrap.ps1 -Check -NoRun` | `./scripts/bootstrap.sh --check --no-run` |

Con `make` disponible: `make setup`, `make check`, `make test`, `make lint`,
`make run ARGS="doctor"`, `make build`, `make clean`. `make help` los lista.

## El flujo de instalación

`axion-wizard install` ejecuta diez pasos en orden, y **persiste el progreso
tras cada uno**: si se interrumpe, la siguiente ejecución se reanuda desde el
último paso completado en vez de empezar de cero.

| # | Paso | Qué hace |
|---|---|---|
| 1 | Entorno | SO, WSL, Docker, hardware → decide la variante de WireGuard (`host` o `ports`) |
| 2 | Red | IP LAN, CGNAT, puertos libres, conectividad saliente |
| 3 | Configuración | Prompts con validación en vivo + resumen y **una sola** confirmación |
| 4 | Certificado | TLS con `subjectAltName`, verificado releyendo el archivo |
| 5 | Compose | `docker-compose.yml`, `.env`, `wg.env`, nginx y el puente FastAPI |
| 6 | Despliegue | `up -d --build` y espera de healthchecks con backoff |
| 7 | Modelo | Descarga del modelo con barra de progreso real |
| 8 | WireGuard | Primer cliente y su QR dibujado en la terminal |
| 9 | Bot y webhook | Pide los tokens de Mattermost (opcional, ver más abajo) |
| 10 | Verificación | Las mismas comprobaciones que `doctor` |

Hasta la confirmación del paso 3 no se escribe **nada** al disco: cancelar
antes no deja nada a medias.

El estado vive en `.axion-wizard-state.json` y guarda solo *qué* pasos
terminaron, nunca sus valores — los secretos no se persisten ahí. Al
reanudar, cada paso reconstruye lo suyo leyendo `.env` y `wg.env`.

### Reanudar no es fiarse del archivo

Ese archivo dice lo que pasó **la última vez**, no lo que hay ahora: entre
dos ejecuciones se puede haber desinstalado Docker, borrado los contenedores
o movido el proyecto. Por eso, antes de dar un paso por hecho, el wizard lo
**comprueba**; si ya no se sostiene, lo rehace — y con él todos los
siguientes, porque se construyeron encima.

Sin eso pasaba lo siguiente, que es un caso real: el estado decía
`deploy: 6 servicios operativos` después de desinstalar Docker, `install` se
saltaba el despliegue —"ya se hizo"— y aterrizaba en el paso 9 a fallar las
siete comprobaciones, sin ninguna pista de que el problema estaba siete
pasos antes.

Al arrancar, si hay progreso guardado, se muestra el mapa completo: qué está
hecho, qué falló y dónde va a retomar.

**Lo que esa comprobación no detecta.** Verifica que lo de la última vez
sigue en pie, no que coincida con lo que este wizard generaría *hoy*: el paso
4 acepta cualquier certificado que tenga algún SAN, y el paso 5 solo mira que
los archivos existan, no su contenido. Así que tras actualizar el wizard, un
`install` a secas puede no aplicar nada y terminar en verde. Para que los
cambios de las plantillas lleguen al despliegue hace falta
`axion-wizard install --restart`, que rehace desde el paso 1 — y regenera el
certificado, así que el navegador volverá a avisar una vez.

### Empezar de cero

```bash
axion-wizard reset               # olvida el progreso; el próximo install va al paso 1
axion-wizard install --restart   # las dos cosas de una vez
```

`reset` borra **solo** el registro de pasos: ni contenedores, ni volúmenes,
ni `.env`, ni el certificado. Rehacer la instalación encima de un despliegue
existente es seguro porque el paso 3 reutiliza la contraseña de PostgreSQL
que ya está en `.env`. Para borrar los datos de verdad es
`axion-wizard uninstall --purge`.

Volver a ejecutar `install` sobre un proyecto ya desplegado es seguro: la
contraseña de PostgreSQL, el token del webhook y las instrucciones de la IA
se conservan del `.env` anterior (Postgres solo aplica su contraseña al
inicializar el volumen; regenerarla dejaría a Mattermost sin poder entrar).
`docker-compose.yml`, `.env` y `wg.env` se respaldan con sufijo `.bak` antes
de reescribirse.

### Modo desatendido (CI)

```bash
axion-wizard install --unattended --config axion.toml
```

```toml
access_mode = "lan"                          # "lan" | "domain"
host = "192.168.1.50"
ollama_model = "qwen2.5:1.5b"
wireguard_admin_password = "panel-seguro"    # se hashea con bcrypt aquí
# wireguard_admin_password_hash = "$2b$..."  # alternativa: hash ya calculado
# postgres_password = "..."                  # opcional; si falta se genera (hex)
# mm_bot_token = "..."                       # opcional; si se conocen de antemano
# mm_webhook_token = "..."                   # (p.ej. reinstalando sobre el mismo Mattermost)
```

Sin esos dos últimos, el paso 9 (bot y webhook) se omite sin más — no hay
prompt que hacer sin terminal, y se aplican después con `set-bot-token`/
`set-webhook-token` igual que en el camino interactivo.

### Interfaz a pantalla completa

```bash
axion-wizard install --tui
```

Un formulario para la configuración y una pantalla con los diez pasos y su
log. Es **una alternativa, no el camino por defecto**: la §1.3 de la spec
descarta Textual para el flujo lineal y esa decisión se mantiene. No se puede
combinar con `--unattended` ni con la entrada redirigida.

## Comandos del wizard

```
axion-wizard                      Flujo completo de instalación
axion-wizard reset                Olvida el progreso: el próximo install va al paso 1
axion-wizard doctor               Re-valida un stack ya desplegado, sin tocarlo
axion-wizard network-check        Solo las verificaciones de red (§4.2)
axion-wizard gen-cert <host>      Genera el certificado TLS
axion-wizard model                Qué modelo e instrucciones usa la IA ahora
axion-wizard model choose         Elige el modelo de una lista y lo aplica
axion-wizard model set <n>        Cambia el modelo: descarga + .env + recrear
axion-wizard model prompt "<t>"   Edita las instrucciones permanentes de la IA
axion-wizard set-bot-token <t>    Quita el límite de 30s: responde al terminar
axion-wizard models               Modelos de Ollama compatibles con este hardware
axion-wizard models pull <n>      Descarga un modelo (sin activarlo)
axion-wizard wireguard add-client <n>   Crea un cliente y muestra su QR
axion-wizard up [servicio]        docker compose up -d (reinicia nginx si toca)
axion-wizard down                 docker compose down
axion-wizard logs [servicio]      Últimas líneas del log de cada servicio
axion-wizard uninstall [--purge]  Baja el stack (--purge borra los volúmenes)
```

## Editar la IA

Cambiar el modelo son tres cosas, no una: descargarlo, apuntar `OLLAMA_MODEL`
a él y **recrear** el contenedor de FastAPI (un `restart` no vale — las
variables de entorno se fijan al crear el contenedor, así que el valor viejo
sobrevive al reinicio). Olvidar el tercer paso deja una IA que sigue
respondiendo con el modelo anterior, sin ningún error.

`axion-wizard model` hace los tres:

```bash
axion-wizard model                       # qué usa ahora mismo
axion-wizard model choose                # elegir de una lista según tu hardware
axion-wizard model set llama3.2:3b       # o directamente por nombre
```

Las instrucciones permanentes —tono, idioma, qué es y qué no debe hacer— se
editan igual, y aplican a toda conversación sin repetirlas cada vez:

```bash
axion-wizard model prompt "Eres el asistente interno de AXION. Responde en español y sé breve."
axion-wizard model prompt ""             # borrarlas
```

### Que la IA pueda tardar lo que necesite

Mattermost espera la respuesta HTTP del webhook saliente y la **abandona a
los ~30 segundos**. Un modelo de 7B en CPU pasa de ahí sin esfuerzo, así que
la respuesta se pierde entera: desde fuera parece que la IA no contesta, y no
hay nada en los logs que lo explique — por dentro el modelo respondió bien.

La solución es que el puente conteste al webhook al instante y publique la
respuesta en el canal cuando el modelo termine. Para eso necesita un bot:

1. Mattermost → **Integraciones → Cuentas de bot → Crear** (si la opción no
   aparece: Consola del sistema → Integraciones → habilitar cuentas de bot).
2. Copiar su token y añadir el bot al equipo y a los canales donde deba
   responder.
3. Pegarlo en el paso 9 del propio `install` (pide justo esto), o después con
   `axion-wizard set-bot-token <token>`.

El `install` no puede crear el bot por ti — Mattermost no expone su API sin
una sesión ya iniciada por un admin humano, y esa cuenta se crea en la propia
interfaz web — pero sí se detiene a mitad de la instalación para pedir el
token en cuanto lo tengas, en vez de dejarlo para después. Dejarlo en blanco
ahí no rompe nada: se aplica más tarde exactamente igual.

A partir de ahí no hay techo de tiempo y puedes usar el modelo que el
hardware aguante. Sin token, el puente sigue funcionando en modo síncrono
exactamente como antes.

Mientras ese token no esté puesto, el wizard sube el plazo que Mattermost
concede al webhook de sus 30 segundos por defecto a **180**
(`MM_SERVICESETTINGS_OUTGOINGINTEGRATIONREQUESTSTIMEOUT`). No sustituye al
modo asíncrono —la petición sigue esperando— pero es la diferencia entre que
un modelo mediano en CPU funcione o pierda cada respuesta. Como referencia
medida en un i3-10100F sin GPU: `qwen2.5:0.5b` genera 19 tokens/s y
`qwen2.5:3b` 4,6, o sea ~43 segundos para una respuesta de 200 tokens.

## Copias de seguridad

El servicio `backup` archiva los volúmenes en `backups/`, dentro del propio
directorio del proyecto, sin que haya que configurar nada.

```
BACKUP_CRON_EXPRESSION=0 3 * * *   # en .env; `install` conserva lo que pongas
BACKUP_RETENTION_DAYS=7
```

Se aplica con `axion-wizard up backup`. Para lanzar una copia ahora mismo:

```bash
docker exec axion-backup-1 backup
```

Dos cosas que conviene saber antes de que pasen:

- **Durante la copia se paran PostgreSQL y Mattermost**, unos segundos, y se
  vuelven a arrancar solos. Copiar el directorio de datos de una base en
  marcha produce un archivo que puede no restaurar; de ahí la hora de
  madrugada por defecto.
- **No se copia `ollama_data`** —son gigabytes de modelo que se recuperan con
  `axion-wizard model set`— ni los logs de Mattermost. Sí se copian la base de
  datos, los archivos subidos, la configuración, los plugins y las claves de
  WireGuard.

El borrado por antigüedad solo alcanza a los archivos que empiezan por
`axion-`, así que se puede dejar cualquier otra cosa en esa carpeta.

## n8n

Va incluido de forma nativa, sin flag: `install` lo despliega junto al resto.
Queda en `http://<host>:5678`, en su propio puerto y **sin pasar por nginx**,
igual que el panel de WireGuard: nadie termina TLS por él, así que se anuncia
como `http` a propósito — decir `https` le haría generar URLs de webhook que
no responden.

Tres cosas que el wizard resuelve por ti y que a mano se pagan caro:

- **`n8n:5678` va en la lista de destinos permitidos de Mattermost.** Sin eso,
  su protección SSRF descarta el webhook saliente **en silencio**: no dispara
  y no aparece error en ningún log. Como esa variable vive en un servicio
  gestionado, ponerla a mano la pisaría el siguiente `install`.
- **`N8N_ENCRYPTION_KEY` se genera una vez y se conserva.** Si cambia, todas
  las credenciales guardadas en n8n quedan ilegibles para siempre; n8n arranca
  igual y los flujos fallan al autenticarse sin decir por qué.
- **Su volumen entra en las copias de seguridad.** Si no, una restauración
  devolvería el chat entero y n8n vacío.

Ajusta `N8N_TIMEZONE` en `.env` con un nombre IANA
(`America/Argentina/Buenos_Aires`, `Europe/Madrid`): con un valor que no
reconozca, n8n se queda en UTC y los flujos programados disparan a otra hora
sin avisar.

Dentro de la red del stack, n8n ve Ollama en `http://ollama:11434` y Mattermost
en `http://mattermost:8065`.

## GPU

El wizard no se fía de que la GPU exista: **prueba** que Docker puede pasarla
a un contenedor antes de reservarla, porque reservarla sin comprobarlo deja a
`ollama` parado en `created` para siempre y arrastra a `fastapi` con él.

| GPU | Qué hace |
|---|---|
| NVIDIA | Prueba `--gpus all`. Necesita `nvidia-container-toolkit`. |
| AMD | Prueba `/dev/kfd` y `/dev/dri`, y usa la imagen de Ollama compilada contra ROCm. Necesita el módulo `amdgpu` y pertenecer a los grupos `video` y `render`. |
| Intel | Se detecta y se avisa: Ollama no publica ninguna imagen para sus GPUs, así que corre en CPU. |

Si la prueba falla, no se rompe nada: el modelo corre en CPU y el aviso dice
qué revisar en cada caso.

## Dónde escribe sus archivos

Sin `--project-dir`, el wizard nunca escribe en el directorio desde el que se
ejecuta el binario a secas: si ese directorio ya tiene un despliegue
(`docker-compose.yml` presente) lo usa tal cual, y si no, crea una subcarpeta
`axion/` ahí y trabaja dentro de ella. Ejecutar el `.exe` recién descargado
directamente desde `~/Descargas`, por ejemplo, crea `~/Descargas/axion/` en
vez de esparcir `docker-compose.yml`, `.env`, `nginx/`… sueltos en Descargas.

Para elegir la carpeta a mano: `axion-wizard --project-dir <ruta> install`.

## Cada despliegue tiene su propio nombre de proyecto

`.env` lleva `COMPOSE_PROJECT_NAME`, generado una vez y conservado en cada
`install` posterior. Es lo que evita que **dos instalaciones distintas en el
mismo host Docker** terminen compartiendo contenedores y volúmenes: Compose
identifica un proyecto por su nombre, no por la carpeta desde la que se
invoca, así que sin un nombre único por despliegue, instalar en una segunda
carpeta reutilizaría los mismos contenedores y volúmenes que el primero —
mismo Postgres, mismo Mattermost, misma base de datos — y cada `install`
sobrescribiría la configuración del otro en silencio.

No es hipotético: pasó en el desarrollo de este proyecto. Una instalación
nueva generó una contraseña de PostgreSQL distinta a la que el volumen ya
tenía inicializada, y Mattermost quedó en bucle de reinicio autenticando con
la contraseña vieja contra un `.env` con la nueva, sin que ningún log
señalara que el problema real era una colisión entre dos instalaciones. Los
datos no se pierden en este escenario —Postgres ignora una contraseña nueva
si el volumen ya estaba inicializado— pero el stack no arranca hasta corregir
cuál `.env` manda.

**Nunca copiar `COMPOSE_PROJECT_NAME` de un despliegue a otro.**

### Mover el despliegue de carpeta

Copiar los archivos y volver a levantar basta: el nombre de proyecto viaja en
`.env`, no depende de la ruta. Si vienes de una instalación con el
`docker-compose.yml` viejo (versiones anteriores a este cambio fijaban
`name: axion` ahí, igual para todo el mundo), el wizard migra ese valor a
`.env` automáticamente en el primer `install` tras actualizar — sin flags ni
pasos manuales.

## Si la IA solo responde al recargar (F5)

Ese síntoma no es que la IA no conteste: contesta, y el mensaje no llega al
navegador. Mattermost empuja los mensajes nuevos por **WebSocket**, y al
recargar la página los vuelve a pedir por HTTP normal — por eso aparecen de
golpe. Es decir: HTTP sano, WebSocket roto.

```bash
axion-wizard doctor    # mirar la fila `WebSocket Mattermost`
```

Esa comprobación hace el handshake de verdad y separa las dos causas, que
piden arreglos opuestos:

| Resultado | Causa | Qué hacer |
|---|---|---|
| Rechazado con HTTP 4xx | `MM_SITEURL` no coincide con el host que usa el navegador, o nginx sin las cabeceras `Upgrade`/`Connection` | Corregir `MM_SITEURL` en `.env` y `axion-wizard up` |
| No responde / se corta | Bug abierto de WSL2 con `networkingMode=mirrored`: las conexiones TCP largas se cuelgan ([moby/moby#48201](https://github.com/moby/moby/issues/48201)) | Volver a NAT + `netsh portproxy`, o convivir con el F5 |

Mirrored es lo que da acceso desde el móvil y otros equipos de la LAN, así que
volver a NAT tiene su propio coste: conviene confirmar cuál de las dos causas
es antes de tocarlo.

Opciones de `install`: `--unattended`, `--config <axion.toml>`, `--tui`.

Opciones globales (van **antes** del subcomando): `--verbose`, `--quiet`,
`--no-color`, `--dry-run`, `--yes`, `--no-elevate`, `--project-dir <ruta>`.

```bash
axion-wizard --project-dir /srv/axion doctor    # correcto
axion-wizard doctor --project-dir /srv/axion    # error: No such option
```

## Privilegios

`install`, `up`, `down` y `uninstall` necesitan administrador (firewall,
`sysctl`, `netsh portproxy`). El wizard explica por qué antes de pedirlo y
relanza el proceso elevado:

- **Windows**: abre un proceso nuevo con UAC —Windows no permite elevar uno ya
  en marcha— y el proceso original **espera a que termine** para propagar su
  código de salida. La ventana elevada pide Enter antes de cerrarse, para que
  su salida se pueda leer.
- **Linux/macOS**: `sudo -E` en la misma terminal.

`--no-elevate` continúa sin privilegios (algunos pasos fallarán) y `--dry-run`
nunca eleva, porque no toca el sistema.

En Linux nativo (variante `host`) los privilegios se usan de verdad para una
cosa concreta: escribir `/etc/sysctl.d/99-wireguard.conf` y activar el
reenvío IP. Sin él, el túnel de WireGuard se establece, el handshake funciona
y el panel muestra el cliente conectado — pero no pasa un solo paquete, y no
aparece error en ningún log. `axion-wizard doctor` lo comprueba en la fila
`Reenvío IP (WireGuard)`.

Variable de entorno `AXION_NO_PAUSE=1`: desactiva la pausa de "Pulsa Enter para
cerrar". Útil en CI o en wrappers. La pausa ya se desactiva sola cuando la
salida no es una terminal interactiva.

## Empaquetado

Genera un binario autocontenido, sin Python en la máquina de destino:

```powershell
.\build\build.ps1        # -> dist\axion-wizard.exe
```

```bash
./build/build.sh         # -> dist/axion-wizard-linux-x86_64
```

No hay cross-compilation: cada plataforma construye el suyo. El script deja el
SHA-256 en `dist/checksums.txt`, verificable con `sha256sum -c`.

## Desarrollo

```bash
.venv/bin/python -m pytest -q          # tests
.venv/bin/python -m ruff check .       # lint
.venv/bin/python -m mypy src           # tipos
```

En Windows, `.venv\Scripts\python.exe`.

## Licencia

Apache-2.0. Ver [LICENSE](LICENSE).

El `.exe`/binario generado por PyInstaller empaqueta dependencias de terceros
bajo licencias MIT, BSD-3-Clause y Apache-2.0; sus avisos de copyright están
en [THIRD-PARTY-LICENSES.txt](THIRD-PARTY-LICENSES.txt).
