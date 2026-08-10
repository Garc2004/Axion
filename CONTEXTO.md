# Contexto para retomar en otro chat

Documento de traspaso. Recoge el estado real de la instalación, lo que se
cambió y lo que quedó pendiente. Reescrito el **10 de agosto de 2026**;
sustituye por completo a la versión del día 9, cuyas rutas ya no existen.

---

## 1. Qué es esto

`axion-wizard` es un instalador/orquestador en Python del stack **AXION**:
Mattermost + WireGuard + Ollama + un puente FastAPI + copias de seguridad
automáticas, todo sobre Docker Compose. Se distribuye como binario
autocontenido (PyInstaller) y ejecuta diez pasos con progreso persistido para
poder reanudarse.

- **Código fuente**: `C:\Users\Perseus\Downloads\Owl\axion-wizard`
- **Despliegue real del usuario**: `C:\Users\Perseus\axion` ← **cambió el 10/08**
- `dist/` ya **solo** contiene artefactos de build. `make clean` vuelve a ser
  seguro.
- Documentación de uso: `README.md`

---

## 2. Estado de la máquina

| | |
|---|---|
| Host de acceso | `192.168.1.65` (interfaz `Ethernet`, red **Private**) |
| SO | Windows 11 Pro |
| RAM | 16 GB — WSL usa **12 GB** (antes 7,7) |
| CPU | Intel i3-10100F, 4 núcleos / 8 hilos |
| GPU | GTX 650 (Kepler, 2012) → sin passthrough, Ollama corre en CPU |
| Docker | 29.6.2, Docker Desktop · Compose v5.3.1 |
| WSL | Ubuntu + docker-desktop, modo `mirrored` |
| Modelo de IA | **`qwen2.5:3b`** (antes `qwen2.5:0.5b`) |

Ocho contenedores, todos con prefijo `axion-`: `postgres`, `mattermost`,
`ollama`, `fastapi`, `nginx`, `wireguard`, **`backup`** y **`n8n`**.

### `.wslconfig` actual

```ini
[wsl2]
networkingMode=mirrored
memory=12GB

[experimental]
hostAddressLoopback=true
```

Copias: `.wslconfig.antes-de-12gb` y `.wslconfig.axion-bak`.

---

## 3. Estado: todo aplicado y verificado

A diferencia del traspaso anterior, **no queda nada pendiente de aplicar**.
`axion-wizard doctor` da las 9 filas en verde con los 7 servicios.

Los tres pendientes que arrastraba el documento del día 9 están cerrados y
comprobados releyendo del contenedor, no del archivo:

| Cambio | Comprobación |
|---|---|
| `MM_SERVICESETTINGS_ALLOWCORSFROM` | `printenv` devuelve los tres orígenes |
| SAN del certificado con `localhost` | nginx sirve `DNS:localhost, IP:127.0.0.1` |
| Escape de `$` en `wg.env` | el hash llega con sus 60 caracteres |

La prueba que lo cierra: el WebSocket con `Origin: https://localhost` devuelve
**101** donde antes daba 403. Ya se puede entrar por `https://localhost`
además de por la IP.

---

## 4. Rendimiento de la IA: el dato que condiciona todo

Medido en este equipo, sin GPU, con el modelo ya cargado:

| Modelo | Velocidad | Respuesta de 200 tokens |
|---|---|---|
| `qwen2.5:0.5b` | 19,1 tokens/s | ~10 s |
| `qwen2.5:3b` | 4,6 tokens/s | ~43 s |

Mattermost abandona el webhook saliente a los **30 s** por defecto y la
respuesta se pierde entera, sin error en ningún log. Con esas cifras, el 0.5b
ya rozaba el techo y el 3b lo habría superado siempre.

Por eso el wizard ahora sube ese plazo a **180 s** vía
`MM_SERVICESETTINGS_OUTGOINGINTEGRATIONREQUESTSTIMEOUT`, que es una variable
del servicio gestionado `mattermost`.

**El modo asíncrono sigue sin activarse**: `MM_BOT_TOKEN` está vacío y el
puente responde `"mode":"sync"`. Es la mejora pendiente de más valor —elimina
la espera del todo y permitiría subir a `qwen2.5:7b`, que ya cabe en los 12 GB
de RAM. Requiere crear un bot a mano en Mattermost (§7).

---

## 5. Trabajo hecho el 10 de agosto

Suite: **819 tests** en verde, `ruff` y `mypy` limpios.

### Fallos encontrados y corregidos

1. **`install` dejaba el stack entero en 502.** nginx resuelve `mattermost`
   una sola vez, al arrancar. Al recrear Mattermost (añadir `ALLOWCORSFROM`
   basta) el contenedor nuevo levanta con otra IP y Compose deja a nginx
   corriendo, apuntando a una IP muerta. Ningún healthcheck lo detecta:
   todos comprueban cada contenedor por dentro y ninguno atraviesa nginx.
   - Arreglo de fondo: nginx re-resuelve en **cada petición** contra el DNS
     interno de Docker (`resolver 127.0.0.11` + variable en `proxy_pass`).
     Probado forzando un cambio de IP real: 200 sin tocar nginx.
   - Y el wizard reinicia nginx tras recrear un upstream suyo, para que
     además relea el certificado.
2. **`gen-cert` dejaba el certificado viejo en servicio.** Generaba el nuevo
   en disco y nunca avisaba a nginx, que sirve el que cargó en memoria.
3. **Mover la carpeta del proyecto perdía todos los volúmenes.** Compose
   deducía el nombre del proyecto del nombre del directorio. Ahora el compose
   fija `name: axion`, y el merge lo **impone** aunque el archivo existente
   traiga otro (una edición a mano ahí reapunta el stack a otros volúmenes).
4. **La reanudación auto-verificada es más débil de lo que decía el
   documento anterior.** El paso 4 acepta cualquier certificado con algún SAN
   y el paso 5 solo mira que los archivos existan, no su contenido. Por eso un
   `install` tras cambiar las plantillas puede no aplicar nada y acabar en
   verde. **No está arreglado**; está documentado en el README y el remedio es
   `install --restart`.

### Funciones nuevas

- **Copias de seguridad automáticas** (`offen/docker-volume-backup:v2.48.2`),
  servicio gestionado más. Archiva a `backups/` a las 3:00, retención 7 días,
  ambos configurables en `.env` y conservados entre instalaciones. Para
  PostgreSQL y Mattermost mientras archiva. Excluye `ollama_data` y los logs.
- **Soporte GPU AMD e Intel**. Antes se probaba `--gpus all` para cualquier
  fabricante, que es del runtime de NVIDIA y da negativo siempre en AMD. Ahora
  cada uno se prueba por su mecanismo (`/dev/kfd` + `/dev/dri` para ROCm), con
  su imagen (`ollama/ollama:0.6.5-rocm`) y su `group_add`. Intel se detecta y
  se avisa de que Ollama no publica imagen para sus GPUs.
- **Plazo del webhook saliente subido a 180 s** (§4).
- **n8n opcional** (`install --with-n8n`), §8.
- **El prompt del modelo respeta el que ya está instalado.** Venía marcado
  sobre la recomendación del catálogo, así que quien hacía
  `model set qwen2.5:3b` y luego reinstalaba perdía su elección con solo
  pulsar Enter. Mismo patrón que la contraseña de PostgreSQL y el token del
  webhook. Ojo: en modo `--unattended` manda el TOML, así que hay que
  mantenerlo al día (pasó hoy: un `install` devolvió el modelo a 0.5b).

### Cambios en la máquina

- WSL de 7,7 a 12 GB.
- Modelo de `qwen2.5:0.5b` a `qwen2.5:3b`.
- Despliegue movido de `dist/` a `C:\Users\Perseus\axion`, migrando los ocho
  volúmenes de `dist_*` a `axion_*`. Verificado: tamaños y número de archivos
  idénticos, y la base conserva 5 usuarios, 1 equipo, 3 canales y 34 mensajes.

---

## 6. Compatibilidad con otras máquinas

Comprobado el 10/08 contra los manifiestos reales de las imágenes.

- **Ubuntu / Linux nativo es el camino mejor soportado**, no el peor: variante
  `host` para WireGuard, sin `netsh portproxy`, sin el bug de mirrored
  networking, sin techo de RAM de WSL. Requiere **Docker Engine, no Docker
  Desktop**: con Desktop el contexto pasa a `desktop-linux` y se cae a la
  variante `ports`.
- **Hueco conocido**: la elevación se pide citando «abrir puertos en el
  firewall (`ufw`)», pero **no hay código que ejecute `ufw` ni reglas de
  firewall en Windows**. Con `ufw enable` hay que abrir 80, 443 y 51820/udp a
  mano.
- **ARM64 no es viable**: `mattermost-team-edition:10.5.1` es **solo amd64**.
  Todo lo demás del stack es multiarquitectura. El `.spec` además fija el
  nombre `axion-wizard-linux-x86_64` compiles donde compiles (cosmético).
- Intel y AMD de 64 bits son indistintos.

---

## 6.b Prueba de ciclo completo (10/08, al cierre)

Se desmontó el stack (`down`, sin tocar volúmenes) y se reinstaló entero con
`install --restart --unattended`. Resultado: **«Instalación completada»**, las
9 comprobaciones en verde con 8 servicios, y la base con sus 5 usuarios y 34
mensajes intactos.

Dos comportamientos quedaron demostrados de paso:

- **`--with-n8n` es aditivo**: no se pasó el flag y n8n se desplegó igual,
  porque el paso 5 lo lee del `docker-compose.yml` existente.
- **Las 5 claves de `PRESERVED_ENV_KEYS` sobrevivieron** a la regeneración del
  `.env`, `N8N_ENCRYPTION_KEY` incluida.

**Las copias de seguridad restauran de verdad.** Se extrajo `postgres_data` de
un archivo real, se arrancó un PostgreSQL desechable encima y devolvió los
mismos 5 usuarios, 1 equipo, 3 canales y 34 mensajes. No es una suposición.

Los 8 volúmenes `dist_*` de la migración ya se borraron; quedan solo los
`axion_*`.

---

## 6.c Publicado en GitHub (10/08)

`https://github.com/Garc2004/Axion`, licencia Apache-2.0, con
`THIRD-PARTY-LICENSES.txt` (avisos de copyright de las dependencias
empaquetadas en el `.exe`: MIT, BSD-3-Clause, Apache-2.0 — ninguna copyleft) y
`assets/logo.png` (recortada la marca de agua de Gemini).

`dist/` está en `.gitignore` — el código fuente no lleva binarios. Si en
algún momento se publica un release con el `.exe`/binario de Linux, **adjuntar
siempre `checksums.txt`** (lo genera `build.ps1`/`build.sh`). Ojo: el SHA-256
prueba integridad, no evita que Smart App Control o un antivirus marquen el
binario — eso depende de firma y reputación, no del hash (ver §10).

Nota para quien siga desde otro chat: al rebasar sobre el `Initial commit`
automático de GitHub (que traía su propio LICENSE/README), `git checkout
--ours` tomó el lado equivocado — en un rebase "ours" es la rama sobre la que
se rebasa, no el commit que se está aplicando, al revés que en un merge
normal. Se corrigió recuperando los archivos del commit original vía
`git reflog` antes de hacer push. Si hay que rebasar de nuevo, usar
`--theirs` para quedarse con el propio trabajo, o mejor: verificar con
`git diff` antes de confiar en cuál lado es cuál.

---

## 6.d Bugfix v0.1.1 y paso 9 nuevo — bot/webhook (10/08, cierre)

**v0.1.1 corrigió un bug crítico de v0.1.0**: `docker-compose.yml` fijaba
`name: axion` igual para toda instalación, así que dos despliegues
cualquiera en el mismo host Docker terminaban compartiendo contenedores y
volúmenes. Pasó de verdad: una instalación de prueba en `C:\Users\Perseus\
Downloads` (sin carpeta dedicada) se enganchó a los volúmenes del despliegue
real, generó una contraseña de Postgres nueva y dejó a Mattermost en bucle de
reinicio. Los datos nunca corrieron peligro (Postgres ignora una contraseña
nueva si el volumen ya está inicializado), pero el stack no arrancaba.

Corregido de raíz — detalle en el
[commit](https://github.com/Garc2004/Axion/commit/4205b44):

- `COMPOSE_PROJECT_NAME` en `.env`, único por despliegue, con migración
  automática del `name:` literal de instalaciones anteriores
  (`resolve_compose_project_name` en `s05_compose.py`).
- Sin `--project-dir`, el wizard ya no escribe en el directorio de trabajo a
  secas: usa una subcarpeta `axion/`, salvo que ese directorio ya tenga un
  despliegue (`docker-compose.yml` presente).
- n8n pasó a ser **nativo**, sin `--with-n8n`: un servicio gestionado más.
- CRLF corrupto en casi todo el árbol (un `git checkout` de una sesión
  anterior lo había roto con `core.autocrlf=true`) y `.gitattributes`
  ampliado para que no vuelva a pasar. Aislado también el cwd de cada test:
  algunos con mocking incompleto escribían en la raíz del repo.

v0.1.0 quedó marcado como *pre-release* en GitHub con un aviso apuntando a
v0.1.1.

**Después, mismo día**: nuevo **paso 9**, "Bot y webhook de Mattermost"
(`s08b_bot_setup.py`, entre WireGuard y la verificación final — total, diez
pasos). No automatiza la creación del bot/webhook en sí —Mattermost no
expone su API sin una sesión ya iniciada por un admin humano, así que no hay
forma de rodear la interfaz web—, pero el `install` se detiene a mitad de
camino a pedir los tokens en cuanto se tienen, en vez de dejarlo para dos
comandos aparte después. Reutiliza la misma escritura que
`set-bot-token`/`set-webhook-token` (`.env` + recrear `fastapi`, una sola vez
para los dos). En `--unattended` los toma de `mm_bot_token`/
`mm_webhook_token` en el `axion.toml` si están; si no, se omite sin más. Se
probó en vivo contra el despliegue real (modo desatendido, con el token de
webhook ya existente) antes de darlo por bueno.

Se planteó automatizar la creación del bot/webhook por la API de Mattermost
(crear el admin inicial vía `mattermost user create` dentro del contenedor,
loguearse, crear todo por REST) — es técnicamente viable y quedó descartado
a favor de esto por ser mucho menos código para el mismo resultado práctico.
Si algún día se retoma, la nota queda aquí.

---

## 6.e Bugfix v0.2.1 — el alta de cliente WireGuard fallaba siempre

Al probar el paso 8 en una instalación real: `POST /api/wireguard/client`
devolvió `{"success":true}` y `_extract_client_id` no encontró ningún campo
reconocible — error de despliegue, instalación cortada ahí.

No era un cambio de contrato, como decía el mensaje de error: es como wg-easy
v14 se comportó **siempre**. Se verificó contra el código fuente real del
proyecto (`github.com/wg-easy/wg-easy`, rama/tag v14): la ruta del servidor
construye el cliente completo por dentro y **descarta el valor de vuelta**,
devolviendo solo `{"success":true}`. El propio frontend oficial de wg-easy
hace lo mismo que la corrección: ignora esa respuesta y vuelve a listar. Dato
importante que cambia el diseño: wg-easy **no exige nombres de cliente
únicos**, así que comparar por nombre habría sido ambiguo.

Arreglado en `services/wireguard.py`: `create_client` ahora lista los
clientes antes de crear, crea, vuelve a listar, y toma el `id` que aparece
en la segunda lista y no en la primera (`_pick_new_client_id`, con
desempate por nombre y luego por `createdAt` para el caso de alta
concurrente). `_extract_client_id` —código muerto ahora— se borró. 30 tests
en `test_wireguard.py`, reescritos donde hacía falta.

**Sin verificar en vivo por quien escribe esto**: no hay contraseña en claro
del panel a mano, solo el hash bcrypt — sin ella no hay forma de loguearse
contra la API real para probarlo de punta a punta. Queda pendiente confirmar
en la próxima ejecución real de `axion-wizard install` (retoma en el paso 8).

---

## 6.f Bot creado, y respuesta en hilo vs. mensaje normal (10/08, cierre)

El usuario creó el bot, lo sumó al canal, y le pasó el segundo obstáculo real
de Mattermost con bots: **un bot recién creado no pertenece a ningún
equipo**, y sumarlo directo a un canal falla con *"1 user was not selected
because they are not a part of this team"*. Hay que añadirlo primero al
equipo desde Consola del sistema → Administración de usuarios → Equipos.
Documentado en el README junto al resto del flujo del bot.

Con el bot funcionando, la respuesta llegó **en hilo** (plegada bajo el
mensaje que la disparó, "1 reply" a un clic de abrir) — comportamiento a
propósito de `answer_in_background` en `fastapi/main.py`
(`root_id=post_id`, para no llenar el canal de mensajes sueltos), pero el
usuario prefería poder elegirlo. Se añadió `AI_REPLY_IN_THREAD` (`.env`,
`true`/`false`, por defecto `true` — no cambia nada para quien ya lo tenía
desplegado):

- `fastapi/main.py`: nueva constante `AI_REPLY_IN_THREAD`; si es `false`,
  `answer_in_background` publica con `root_id=""` en vez del id del mensaje
  original. Sin efecto en modo síncrono (ahí decide el propio mecanismo de
  webhooks salientes de Mattermost, no este código).
- `docker-compose.yml.j2`: `AI_REPLY_IN_THREAD: ${AI_REPLY_IN_THREAD:-true}`
  en el servicio `fastapi`.
- `env.j2` / `PRESERVED_ENV_KEYS` / `render_env`: mismo patrón que
  `BACKUP_CRON_EXPRESSION` — declarado siempre, con valor por defecto,
  conservado entre instalaciones.
- **Paso 9** (`s08b_bot_setup.py`): con token de bot puesto, pregunta además
  "¿en hilo o mensaje normal?" (`questionary.confirm`, default `True`) y lo
  escribe en el mismo lote que los tokens —un solo recreate de fastapi—. Sin
  bot no se pregunta: sin modo asíncrono el ajuste no tiene ningún efecto. En
  `--unattended` sale de `ai_reply_in_thread` en el `axion.toml` (booleano o
  string), y si no está, no se fuerza nada.

Para cambiarlo en un despliegue que ya pasó el paso 9 (no se vuelve a
preguntar solo, `revalidate_on_resume = False`): editar `AI_REPLY_IN_THREAD`
en `.env` a mano y `axion-wizard up fastapi`.

14 tests nuevos entre `test_fastapi_bridge.py`, `test_compose_render.py` y
`test_steps.py`.

---

## 7. Pendientes

1. **Crear el bot en Mattermost.** El *paso* 9 (§6.d) ya se detiene a pedir el
   token durante el propio `install`, pero crear el bot en sí sigue
   requiriendo la interfaz web —Mattermost no expone su API sin una sesión ya
   iniciada por un admin humano—, así que sigue siendo lo de más valor
   pendiente: quita el techo de tiempo y abre la puerta a `qwen2.5:7b`.
   Integraciones → Cuentas de bot → Crear; el token se pega en el paso 9 del
   siguiente `install --restart`, o con `axion-wizard set-bot-token <token>`
   sin esperar a reinstalar.
2. **Rotar el token del webhook.** El valor real (está en `.env`) llegó a estar
   escrito en los tests. Rotar en *Integraciones → Webhooks salientes* y
   aplicar con `axion-wizard set-webhook-token <nuevo>`, o pegarlo en el
   paso 9 del próximo `install`.
3. **Montar la vigilancia que falta.** Con las copias resueltas, lo siguiente
   por orden de utilidad: Dozzle (logs), Uptime Kuma (avisos), RAG. **No**
   Watchtower (choca con la prohibición de `:latest`) ni Prometheus/Grafana
   (desproporcionado para una LAN).
4. **Sacar las copias del mismo disco.** Restauran bien (§6.b), pero viven
   junto a lo que protegen: un fallo del disco se lleva las dos cosas.
5. **Firmar el `.exe`**, si el bloqueo de Smart App Control se repite (§10).

---

## 8. n8n: instalado y funcionando

`axion-wizard install --with-n8n`. Está desplegado y verificado en
`http://192.168.1.65:5678`. Los cinco escollos del plan original, resueltos:

1. **SSRF de Mattermost**: `n8n:5678 n8n` va en
   `MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS`, y el valor sale
   ahora de la constante `SSRF_ENV_VALUE` en vez de estar escrito a mano en la
   plantilla (estaban duplicados y divergieron en cuanto se tocó uno).
   **Comprobado**: `docker exec axion-mattermost-1 curl http://n8n:5678/`
   devuelve 200.
2. **`N8N_ENCRYPTION_KEY`**: generada una vez y en `PRESERVED_ENV_KEYS`.
3. **Tag fijada**: `docker.n8n.io/n8nio/n8n:2.34.4`.
4. **nginx**: se descartó ponerlo detrás. Va en su propio puerto y se anuncia
   como `http`, igual que el panel de WireGuard — nadie termina TLS por él, y
   anunciarse como `https` le haría generar URLs de webhook que no responden.
   Eso evita además el hueco de `X-Forwarded-Host` que señalaba el plan.
5. **Puerto 5678**: no hizo falta tocar las comprobaciones de red ni `doctor`;
   ambos derivan la lista de servicios del compose desplegado, así que n8n
   entró solo (la fila de contenedores pasó de 7 a 8).

Su volumen `n8n_data` entra en las copias de seguridad. El flag es aditivo:
`managed_services_in` lee del compose si n8n está, así que olvidarlo en un
`install` posterior no lo desinstala.

`N8N_TIMEZONE` en `.env` está en **UTC** — conviene ponerle
`America/Argentina/Buenos_Aires` o los cron dispararán a otra hora.

Dentro de `edge_net`, n8n ve Ollama en `http://ollama:11434` y Mattermost en
`http://mattermost:8065`.

---

## 9. Cosas del proyecto que cuesta descubrir

- **`MANAGED_SERVICES`** se regenera en cada `install`; lo que no esté en esa
  lista se conserva. Editar a mano un servicio gestionado no sirve de nada.
- **`MANAGED_TOP_LEVEL_KEYS`** (hoy solo `name`) se **impone** al fusionar, a
  diferencia del resto de claves de nivel superior, que se preservan.
- **`PRESERVED_ENV_KEYS`**: token del webhook, token del bot, prompt del
  sistema y los dos ajustes de copias. La contraseña de PostgreSQL se conserva
  por otra vía, en el paso 3.
- **Nada de `$`, backtick ni `!`** en valores que van a `.env`. Y el `$` en
  `env_file` lo interpola Compose: por eso el hash lleva `$$`.
- **Imágenes siempre con tag fijada.** wg-easy `latest` es v15, que ignora
  `WG_HOST`/`PASSWORD_HASH` sin dar un solo error.
- **La verificación se hace releyendo del otro lado**, no confiando en lo que
  se escribió.
- **Las variables de entorno de Mattermost no reescriben su `config.json`**:
  sobrescriben en memoria. Ver 30 en el archivo y 180 en `printenv` es lo
  normal, no un fallo.
- **`install` sin `--restart` puede no aplicar nada** tras cambiar las
  plantillas (§5.4).

---

## 10. Smart App Control: puede bloquear el binario, y es intermitente

Smart App Control está **activado** en este equipo
(`HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy` →
`VerifiedAndReputablePolicyState = 1`) y rechaza ejecutables sin firma ni
reputación con el mensaje «Una directiva de Control de aplicaciones bloqueó
este archivo».

**No es determinista.** El 10/08 bloqueó una compilación y dejó pasar la
siguiente sin que cambiara nada del código: el veredicto va por el hash del
archivo concreto, así que **una recompilación limpia suele resolverlo**:

```powershell
Remove-Item dist\axion-wizard.exe, build\work -Recurse -Force
.\build\build.ps1
```

Si vuelve a pasar y la recompilación no basta, por orden de sensatez:

1. Usar el binario de Linux dentro de WSL, donde SAC no aplica.
2. Trabajar desde el código fuente, que nunca se ve afectado:
   `.venv\Scripts\python.exe -m axion_wizard --project-dir C:\Users\Perseus\axion doctor`
3. Firmar el `.exe` con un certificado de firma de código.
4. Desactivar Smart App Control. **Es irreversible**: solo se vuelve a activar
   reinstalando Windows. No recomendado.

### Los contenedores pueden desaparecer solos

También el 10/08, y sin acción que lo explique, los 8 contenedores
desaparecieron mientras se compilaba (Docker Desktop y WSL seguían corriendo,
los 9 volúmenes intactos). Se recuperó todo con `axion-wizard up` sin perder
un dato. Si el stack aparece vacío, **antes de reinstalar nada** comprobar que
los volúmenes `axion_*` siguen ahí y probar `up`: la causa más cara sería dar
por perdido lo que solo estaba parado.

---

## 11. Comandos para retomar

```powershell
# Estado del stack
cd C:\Users\Perseus\axion
.\axion-wizard.exe doctor

# Copia de seguridad ahora mismo
docker exec axion-backup-1 backup

# Desarrollo, desde la raíz del proyecto
cd C:\Users\Perseus\Downloads\Owl\axion-wizard
.venv\Scripts\python.exe -m pytest -q        # 819 pasan, 1 se salta
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src

# Recompilar y llevar el binario al despliegue
.\build\build.ps1
Copy-Item dist\axion-wizard.exe C:\Users\Perseus\axion\ -Force
```

Para aplicar cambios de plantilla al despliegue hace falta `--restart`, o
recortar a mano los pasos completados de `.axion-wizard-state.json` (que es lo
que se hizo hoy para no regenerar el certificado en cada pasada).

El binario de Linux se construye en WSL sobre una **copia** del proyecto,
nunca sobre el directorio original: ahí `.venv` es un entorno de Windows y
`python3 -m venv .venv` reescribiría su `pyvenv.cfg`.

```bash
./build/build.sh                             # -> dist/axion-wizard-linux-x86_64
```
