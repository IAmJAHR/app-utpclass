import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.config import BASE_DIR, DEFAULT_GDRIVE_FOLDER_ID, DOWNLOAD_DIR
from src.storage import StateDB

ROOT_FOLDER_ID = DEFAULT_GDRIVE_FOLDER_ID

# Pre-created course folder IDs in 1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx
COURSE_FOLDER_IDS: Dict[str, str] = {
    "ANÁLISIS Y DISEÑO DE SISTEMAS DE INFORMACIÓN (12879)": "1XturJ8khSJyBKjxqnfnWz7FSCv8X8DJx",
    "Crea Apps Web como un Pro_ Domina Node.js y Angular _ THP (26202)": "1A30ELw9-qc3DK5jQyqcIXUJEwcO3D7NA",
    "CURSO INTEGRADOR I_ SISTEMAS - SOFTWARE (21697)": "1P4bZsrHa2JmfK6Q69kMvuWNpTFXb87b9",
    "DISEÑO DE PRODUCTOS Y SERVICIOS (14730)": "1V8oydm0mJCKYD_Li7o4YRRXkYFZVNkTu",
    "HERRAMIENTAS DE DESARROLLO (41991)": "1zxtmS1TtfybZcNWyHmynMj5YZXDsIiwJ",
    "LIDERAZGO Y GESTIÓN DE EQUIPOS (16232)": "1bxIgLXH8uiv-fXOqSnkG6ufmd1yaRKnu",
    "Ruta Laboral (40057)": "1Qt8mOl-PYxNMCQ6KliCAJX681Z6eYm46",
    "SEGURIDAD INFORMÁTICA (12870)": "1-bDWsEDJ6tHonXALd0Z5fEYwqoqN2iWO",
}

ID_RE = re.compile(r"\(ID:\s*([a-zA-Z0-9_-]+)\)")
print_lock = threading.Lock()


def run_gdrive(args: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    cmd = ["gdrive.cmd"] + args
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def gdrive_mkdir(name: str, parent_id: str) -> Optional[str]:
    for attempt in range(3):
        code, out, err = run_gdrive(["mutate", "mkdir", name, "--parent", parent_id])
        if code == 0:
            m = ID_RE.search(out)
            if m:
                return m.group(1)
        time.sleep(0.5 * (attempt + 1))
    return None


def gdrive_upload(file_path: Path, parent_id: str) -> Optional[str]:
    for attempt in range(3):
        code, out, err = run_gdrive(["mutate", "upload", str(file_path), "--parent", parent_id], timeout=180)
        if code == 0:
            m = ID_RE.search(out)
            if m:
                return m.group(1)
        time.sleep(0.6 * (attempt + 1))
    return None


def main() -> None:
    skip_mp4 = "--include-mp4" not in sys.argv
    db = StateDB()

    print(f"=== Iniciando subida masiva a Google Drive ({ROOT_FOLDER_ID}) ===")

    # 1. Ensure all course folders exist
    course_dirs = sorted([d for d in DOWNLOAD_DIR.iterdir() if d.is_dir()])
    for cdir in course_dirs:
        if cdir.name not in COURSE_FOLDER_IDS:
            fid = gdrive_mkdir(cdir.name, ROOT_FOLDER_ID)
            if fid:
                COURSE_FOLDER_IDS[cdir.name] = fid
                print(f"Carpeta de curso creada: {cdir.name} -> {fid}")

    # 2. Discover all week / category subfolders (Level 1 under each course)
    week_folders_to_create: List[Tuple[str, str, str]] = []  # (course_name, week_name, parent_course_id)
    for cdir in course_dirs:
        cid = COURSE_FOLDER_IDS.get(cdir.name)
        if not cid:
            continue
        for sub in sorted(cdir.iterdir()):
            if sub.is_dir():
                week_folders_to_create.append((cdir.name, sub.name, cid))

    print(f"Creando {len(week_folders_to_create)} subcarpetas de semanas/tareas en paralelo...")
    week_folder_ids: Dict[Tuple[str, str], str] = {}

    with ThreadPoolExecutor(max_workers=12) as pool:
        future_map = {
            pool.submit(gdrive_mkdir, wname, pid): (cname, wname)
            for cname, wname, pid in week_folders_to_create
        }
        done_cnt = 0
        for fut in as_completed(future_map):
            cname, wname = future_map[fut]
            fid = fut.result()
            done_cnt += 1
            if fid:
                week_folder_ids[(cname, wname)] = fid
            if done_cnt % 25 == 0 or done_cnt == len(week_folders_to_create):
                print(f"  Subcarpetas creadas: {done_cnt}/{len(week_folders_to_create)}")

    # 3. Collect all files to upload (sorted smallest to largest so documents finish quickly)
    upload_tasks: List[Tuple[Path, str, str, str]] = []  # (file_path, target_parent_id, course_name, rel_folder)
    skipped_mp4: List[Path] = []

    for cdir in course_dirs:
        cid = COURSE_FOLDER_IDS.get(cdir.name)
        if not cid:
            continue
        for f in sorted(cdir.rglob("*")):
            if not f.is_file():
                continue
            if skip_mp4 and f.suffix.lower() == ".mp4":
                skipped_mp4.append(f)
                continue
            rel_to_course = f.relative_to(cdir)
            parts = rel_to_course.parts
            if len(parts) == 1:
                # Root file of course (00_Silabo.pdf, RESUMEN_CURSO.md, Anuncios.md)
                target_pid = cid
                rel_folder = cdir.name
            else:
                week_name = parts[0]
                target_pid = week_folder_ids.get((cdir.name, week_name), cid)
                rel_folder = f"{cdir.name}/{week_name}"
            upload_tasks.append((f, target_pid, cdir.name, rel_folder))

    upload_tasks.sort(key=lambda x: x[0].stat().st_size)
    total_files = len(upload_tasks)
    print(f"Subiendo {total_files} archivos a Google Drive con 12 hilos paralelos...")

    uploaded_records: List[Dict[str, object]] = [
        {
            "file_id": "1W3ruYAYCHRf55EGNUfeAVFVWEZftWLqU",
            "name": "AGENDA_PENDIENTES.md",
            "folder_path": "Raíz",
            "course_folder": "Raíz Drive",
            "mime_type": ".md",
            "size_kb": 1.0,
            "web_view_link": "https://drive.google.com/file/d/1W3ruYAYCHRf55EGNUfeAVFVWEZftWLqU/view",
            "download_link": "/files/AGENDA_PENDIENTES.md",
        }
    ]

    completed = 0
    failed = 0
    start_ts = time.time()

    with ThreadPoolExecutor(max_workers=12) as pool:
        future_to_task = {
            pool.submit(gdrive_upload, fpath, pid): (fpath, pid, cname, rfolder)
            for fpath, pid, cname, rfolder in upload_tasks
        }
        for fut in as_completed(future_to_task):
            fpath, pid, cname, rfolder = future_to_task[fut]
            fid = fut.result()
            completed += 1
            rel_local = fpath.relative_to(DOWNLOAD_DIR).as_posix()
            size_kb = round(fpath.stat().st_size / 1024, 1)
            if fid:
                rec = {
                    "file_id": fid,
                    "name": fpath.name,
                    "folder_path": rfolder,
                    "course_folder": cname,
                    "mime_type": fpath.suffix.lower(),
                    "size_kb": size_kb,
                    "web_view_link": f"https://drive.google.com/file/d/{fid}/view",
                    "download_link": f"/files/{rel_local}",
                }
                uploaded_records.append(rec)
            else:
                failed += 1

            if completed % 40 == 0 or completed == total_files:
                elapsed = time.time() - start_ts
                print(
                    f"  Progreso subida: {completed}/{total_files} archivos "
                    f"(OK: {len(uploaded_records)-1}, Fallidos: {failed}, {elapsed:.1f}s)"
                )

    # Save all real Google Drive file IDs & links into SQLite state.db
    db.replace_gdrive_files(uploaded_records)
    db.set_setting("gdrive_last_sync", time.strftime("%Y-%m-%d %H:%M:%S"))

    # Also save a JSON manifest of all Google Drive IDs and upload it to the root Drive folder
    manifest_path = BASE_DIR / "data" / "gdrive_manifest.json"
    manifest_data = {
        "root_folder_id": ROOT_FOLDER_ID,
        "course_folders": COURSE_FOLDER_IDS,
        "total_uploaded": len(uploaded_records),
        "files": uploaded_records,
    }
    manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")
    m_fid = gdrive_upload(manifest_path, ROOT_FOLDER_ID)
    print(f"Manifiesto gdrive_manifest.json subido a Drive (ID: {m_fid})")
    db.close()
    print("=== ¡Subida a Google Drive completada! ===")


if __name__ == "__main__":
    main()
