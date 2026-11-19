# UTP+ Cloud — Panel Minimalista, Google Drive & Control Admin (Dokploy Ready)

Plataforma web minimalista y en **tiempo real (Server-Sent Events)** para gestionar y explorar todos tus cursos, semanas, materiales, sílabos, tareas y anuncios de **UTP+class**, integrada con **Google Drive** y con un **Panel de Control de Perfiles y Administrador** lista para desplegar en **Dokploy**.

---

## ✨ Novedades Implementadas

1. **Integración con Google Drive (`1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx`)**:
   - Vinculado directamente a tu carpeta: [`https://drive.google.com/drive/folders/1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx?hl=es-419`](https://drive.google.com/drive/folders/1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx?hl=es-419).
   - **Catálogo Híbrido (Google Drive + Local + UTP+ En Vivo)**: Reconstruye instantáneamente (<15ms) todos los cursos, semanas, temas, 790+ archivos, tareas y anuncios desde la carpeta `downloads/` sincronizada con Google Drive, incluso si no hay sesión activa de Playwright en el servidor.
   - **Sincronización desde Google Drive**: Descarga e indexa las carpetas y archivos subidos a tu carpeta de Drive con un clic (`🔄 Sync Drive`).
     - *Requisito en Drive*: Haz clic derecho sobre tu carpeta en Google Drive → **Compartir** → **"Cualquier persona con el enlace (Lector)"** (o configura un `GDRIVE_API_KEY` en el panel Admin).

2. **Diseño Dinámico y Minimalista**:
   - Interfaz limpia estilo Linear/Vercel con modo **Oscuro / Claro** (`🌓`), búsqueda instantánea de cursos y archivos, carga ultrarrápida y cero ruido visual.

3. **Panel de Control de Perfiles y Admin en Tiempo Real**:
   - **Conectar / Desconectar Usuarios Web en Vivo**:
     - Visualiza quién está **`● En línea`** en tiempo real.
     - Como **Administrador**, puedes hacer clic en **`🔌 Desconectar`** sobre cualquier usuario: su pantalla se bloqueará **al instante en tiempo real** sin necesidad de recargar la página. Al hacer clic en **`⚡ Conectar`**, su acceso se restaura automáticamente en vivo.
     - Crea nuevos perfiles de usuario con usuario, nombre visible, PIN/clave y rol (`Usuario` o `Admin`).
   - **Gestión de Múltiples Perfiles UTP+class**:
     - Conecta, desconecta o cambia la cuenta activa de estudiante UTP+class en tiempo real desde la pestaña **🛡️ Perfiles & Admin**.

4. **Listo para Dokploy**:
   - Servidor multihilo escuchando en `0.0.0.0:8080` con soporte `X-Accel-Buffering: no` para streaming SSE en tiempo real a través del proxy Traefik/Nginx de Dokploy.
   - Endpoint de healthcheck en `/api/health`.
   - Incluye `Dockerfile`, `docker-compose.yml` y `.dockerignore`.

---

## 🚀 Despliegue en Dokploy

1. Sube este proyecto a tu repositorio de GitHub/GitLab (o súbelo a Dokploy).
2. Sube el contenido de tu carpeta `downloads/` a tu carpeta de Google Drive:
   - [`https://drive.google.com/drive/folders/1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx?hl=es-419`](https://drive.google.com/drive/folders/1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx?hl=es-419)
   - Asegúrate de que la carpeta en Drive esté compartida como **"Cualquier persona con el enlace (Lector)"**.
3. En **Dokploy**:
   - Crea un servicio tipo **Application (Dockerfile)** o **Docker Compose**.
   - Puerto del contenedor: **`8080`**.
   - Variables de entorno recomendadas en Dokploy:
     ```env
     HOST=0.0.0.0
     PORT=8080
     ADMIN_USERNAME=admin
     ADMIN_PASSWORD=tu_clave_admin
     GDRIVE_FOLDER_ID=1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx
     UTP_USERNAME=tu_codigo_utp
     UTP_PASSWORD=tu_password_utp
     ```

---

## 💻 Uso Local

```powershell
.\.venv\Scripts\python.exe app.py
```
O haz doble clic en `run_ui.bat`. El panel se abrirá en `http://127.0.0.1:8080`.
