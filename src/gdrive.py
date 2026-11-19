import base64
import html
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from .config import DEFAULT_GDRIVE_FOLDER_ID, DOWNLOAD_DIR, ensure_dirs
from .storage import StateDB


FOLDER_ID_RE = re.compile(r"(?:folders/|id=)([a-zA-Z0-9_-]{15,})")


def extract_folder_id(raw_url_or_id: str) -> str:
    raw = (raw_url_or_id or "").strip()
    if not raw:
        return DEFAULT_GDRIVE_FOLDER_ID
    m = FOLDER_ID_RE.search(raw)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{15,}", raw):
        return raw
    return DEFAULT_GDRIVE_FOLDER_ID


def get_folder_url(folder_id: str) -> str:
    fid = extract_folder_id(folder_id)
    return f"https://drive.google.com/drive/folders/{fid}?hl=es-419"


# ---------------------------------------------------------------------------
# 1. Local & Synced Drive Data Parser (Offline / Cloud Catalog Reconstruction)
# ---------------------------------------------------------------------------

def parse_local_catalog(db: Optional[StateDB] = None) -> Dict[str, Any]:
    """
    Reconstructs courses, pending tasks, weeks, themes, announcements, and files
    from DOWNLOAD_DIR (and indexed Google Drive files) so the app works dynamically
    from the synced Google Drive data even without an active UTP+class login.
    """
    ensure_dirs()
    courses: List[Dict[str, Any]] = []
    gdrive_files = db.list_gdrive_files() if db else []
    gdrive_by_course: Dict[str, List[Dict[str, Any]]] = {}
    for gf in gdrive_files:
        cf = gf.get("course_folder") or "Raíz Drive"
        gdrive_by_course.setdefault(cf, []).append(gf)

    course_dirs = sorted([d for d in DOWNLOAD_DIR.iterdir() if d.is_dir()]) if DOWNLOAD_DIR.exists() else []
    seen_folder_names = set()

    for cdir in course_dirs:
        seen_folder_names.add(cdir.name)
        info = _parse_course_folder(cdir, gdrive_by_course.get(cdir.name, []))
        courses.append(info)

    # Also include courses that exist in Google Drive index but aren't downloaded locally yet
    for cf_name, gfiles in sorted(gdrive_by_course.items()):
        if cf_name not in seen_folder_names and cf_name != "Raíz Drive":
            m = re.match(r"^(.*?)\s*\(([^)]+)\)$", cf_name)
            cname = m.group(1).strip() if m else cf_name
            cnum = m.group(2).strip() if m else cf_name[:8]
            courses.append(
                {
                    "courseId": f"gdrive-{cnum}",
                    "sectionId": f"gdrive-{cnum}",
                    "name": cname,
                    "classNumber": cnum,
                    "sectionCode": cnum,
                    "period": "Drive",
                    "modality": "Cloud",
                    "progress": 100,
                    "teacher": "Google Drive Cloud",
                    "teacherEmail": "",
                    "localFolder": str(DOWNLOAD_DIR / cf_name),
                    "folderName": cf_name,
                    "downloadedCount": len(gfiles),
                    "driveCount": len(gfiles),
                    "source": "gdrive",
                }
            )

    tasks = parse_agenda_tasks()
    return {
        "courses": courses,
        "tasks": tasks,
    }


def _parse_course_folder(cdir: Path, drive_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    folder_name = cdir.name
    m = re.match(r"^(.*?)\s*\(([^)]+)\)$", folder_name)
    course_name = m.group(1).strip() if m else folder_name
    class_num = m.group(2).strip() if m else ""

    section_code = class_num
    teacher = "Docente UTP"
    teacher_email = ""
    progress = 0

    resumen_path = cdir / "RESUMEN_CURSO.md"
    if resumen_path.exists():
        try:
            txt = resumen_path.read_text(encoding="utf-8", errors="ignore")
            m_sec = re.search(r"Código de sección:\*\*\s*`([^`]*)`", txt)
            if m_sec:
                section_code = m_sec.group(1).strip()
            m_doc = re.search(r"Docente:\*\*\s*([^\n(]+)(?:\(([^)]*)\))?", txt)
            if m_doc:
                teacher = m_doc.group(1).strip() or teacher
                teacher_email = (m_doc.group(2) or "").strip()
            m_prog = re.search(r"Progreso:\*\*\s*(\d+)%", txt)
            if m_prog:
                progress = int(m_prog.group(1))
        except Exception:
            pass

    local_count = sum(1 for p in cdir.rglob("*") if p.is_file())
    total_available = max(local_count, len(drive_files))
    cid = class_num or re.sub(r"\W+", "_", folder_name).lower()
    return {
        "courseId": cid,
        "sectionId": cid,
        "name": course_name,
        "classNumber": class_num,
        "sectionCode": section_code,
        "period": "Actual",
        "modality": "UTP+ / Drive",
        "progress": progress,
        "teacher": teacher,
        "teacherEmail": teacher_email,
        "localFolder": str(cdir),
        "folderName": folder_name,
        "downloadedCount": total_available,
        "driveCount": len(drive_files),
        "source": "local+drive",
    }


def parse_agenda_tasks() -> List[Dict[str, Any]]:
    agenda_file = DOWNLOAD_DIR / "AGENDA_PENDIENTES.md"
    tasks: List[Dict[str, Any]] = []
    if not agenda_file.exists():
        return tasks

    now = datetime.now()
    try:
        lines = agenda_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in lines:
            line = line.strip()
            if not line.startswith("|") or "---" in line or "Urgencia" in line:
                continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) < 7:
                continue
            _, act_type, course_name, title, week_str, finish_at, score_str = parts[:7]
            week_num = re.sub(r"\D+", "", week_str) or "-"
            top_score = re.sub(r"[^\d.]", "", score_str) or "20"

            hours_left: Optional[float] = None
            if finish_at and finish_at != "-":
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        dt = datetime.strptime(finish_at[:19], fmt)
                        hours_left = (dt - now).total_seconds() / 3600.0
                        break
                    except ValueError:
                        continue

            tasks.append(
                {
                    "type": act_type,
                    "courseName": course_name,
                    "activityTitle": title,
                    "weekNumber": week_num,
                    "finishAt": finish_at,
                    "evaluationTopScore": top_score,
                    "hours_left": hours_left,
                }
            )
    except Exception:
        pass
    return tasks


def parse_course_detail_from_local(
    course_name: str, class_num: str, db: Optional[StateDB] = None
) -> Dict[str, Any]:
    """
    Builds the full course detail view (weeks, themes, contents, tasks, announcements, local & Drive files)
    directly from the local/synced folder in DOWNLOAD_DIR.
    """
    from .downloader import sanitize_name

    expected_folder = sanitize_name(f"{course_name} ({class_num})" if class_num else course_name, 70)
    cdir = DOWNLOAD_DIR / expected_folder
    if not cdir.exists() and DOWNLOAD_DIR.exists():
        # Try matching by class_num or course_name substring
        for d in DOWNLOAD_DIR.iterdir():
            if d.is_dir():
                if (class_num and f"({class_num})" in d.name) or course_name.lower() in d.name.lower():
                    cdir = d
                    break

    current_week = 1
    num_weeks = 18
    resumen_path = cdir / "RESUMEN_CURSO.md"
    if resumen_path.exists():
        try:
            txt = resumen_path.read_text(encoding="utf-8", errors="ignore")
            m_wk = re.search(r"Semana actual:\*\*\s*(\d+)/(\d+)", txt)
            if m_wk:
                current_week = int(m_wk.group(1))
                num_weeks = int(m_wk.group(2))
        except Exception:
            pass

    # 1. Weeks & Themes from folder tree
    weeks_list: List[Dict[str, Any]] = []
    if cdir.exists():
        week_dirs = sorted(
            [d for d in cdir.iterdir() if d.is_dir() and (d.name.startswith("Semana_") or d.name.startswith("00_"))]
        )
        for wdir in week_dirs:
            m_w = re.search(r"(\d+)", wdir.name)
            w_num = int(m_w.group(1)) if m_w else 0
            label = f"Semana {w_num:02d}" if w_num > 0 else "Información General"
            themes = []
            theme_dirs = sorted([d for d in wdir.iterdir() if d.is_dir()])
            if not theme_dirs:
                theme_dirs = [wdir]
            for tdir in theme_dirs:
                contents = []
                for f in sorted(tdir.iterdir()):
                    if not f.is_file():
                        continue
                    rel = f.relative_to(DOWNLOAD_DIR).as_posix()
                    ext = f.suffix.lower()
                    if f.name == "Enlaces.md":
                        try:
                            md_txt = f.read_text(encoding="utf-8", errors="ignore")
                            for lm in re.finditer(r"-\s*\[([^\]]+)\]\((https?://[^)]+)\)", md_txt):
                                contents.append(
                                    {
                                        "contentId": lm.group(2),
                                        "title": lm.group(1),
                                        "type": "URL",
                                        "url": lm.group(2),
                                        "filename": "",
                                        "html": None,
                                    }
                                )
                        except Exception:
                            pass
                    elif ext == ".html":
                        try:
                            h_txt = f.read_text(encoding="utf-8", errors="ignore")
                        except Exception:
                            h_txt = ""
                        contents.append(
                            {
                                "contentId": rel,
                                "title": f.stem,
                                "type": "HTML",
                                "url": f"/files/{rel}",
                                "filename": f.name,
                                "html": h_txt,
                            }
                        )
                    else:
                        contents.append(
                            {
                                "contentId": rel,
                                "title": f.name,
                                "type": "FILE",
                                "url": f"/files/{rel}",
                                "filename": f.name,
                                "html": None,
                            }
                        )
                themes.append(
                    {
                        "themeId": tdir.name,
                        "name": tdir.name if tdir != wdir else label,
                        "weekNumber": w_num,
                        "contents": contents,
                    }
                )
            weeks_list.append(
                {
                    "weekNumber": w_num,
                    "label": label,
                    "themes": themes,
                }
            )

    # 2. Tasks from Tareas_y_Evaluaciones/*.md
    tasks_out: List[Dict[str, Any]] = []
    hw_dir = cdir / "Tareas_y_Evaluaciones"
    if hw_dir.exists():
        for md_file in sorted(hw_dir.glob("*.md")):
            try:
                raw = md_file.read_text(encoding="utf-8", errors="ignore")
                title_m = re.search(r"^#\s+(.+)$", raw, re.M)
                kind_m = re.search(r"-\s*\*\*Tipo:\*\*\s*(.+)$", raw, re.M)
                stat_m = re.search(r"-\s*\*\*Estado:\*\*\s*(.+)$", raw, re.M)
                from_m = re.search(r"-\s*\*\*Disponible desde:\*\*\s*(.+)$", raw, re.M)
                to_m = re.search(r"-\s*\*\*Fecha límite:\*\*\s*(.+)$", raw, re.M)
                score_m = re.search(r"-\s*\*\*Puntaje:\*\*\s*([^/\n]+)/(.+)$", raw, re.M)
                ind_m = re.split(r"##\s+Indicaciones\s*", raw, maxsplit=1)
                desc_txt = ind_m[1].strip() if len(ind_m) > 1 else ""

                sc_val = score_m.group(1).strip() if score_m else "-"
                top_val = score_m.group(2).strip() if score_m else "20"
                tasks_out.append(
                    {
                        "id": md_file.stem,
                        "kind": (kind_m.group(1).strip() if kind_m else "TAREA"),
                        "title": (title_m.group(1).strip() if title_m else md_file.stem),
                        "status": (stat_m.group(1).strip() if stat_m else "REGISTRADA"),
                        "visibleFrom": (from_m.group(1).strip() if from_m else "-"),
                        "visibleTo": (to_m.group(1).strip() if to_m else "-"),
                        "score": None if sc_val == "-" else sc_val,
                        "topScore": top_val,
                        "weekNumber": "-",
                        "description": desc_txt,
                        "descriptionHtml": f"<div style='white-space:pre-wrap'>{html.escape(desc_txt)}</div>",
                    }
                )
            except Exception:
                pass

    # 3. Announcements from Anuncios.md
    anns_out: List[Dict[str, Any]] = []
    ann_file = cdir / "Anuncios.md"
    if ann_file.exists():
        try:
            raw = ann_file.read_text(encoding="utf-8", errors="ignore")
            blocks = re.split(r"^##\s+", raw, flags=re.M)[1:]
            for idx, b in enumerate(blocks):
                lines = b.strip().splitlines()
                atitle = lines[0].strip() if lines else "Anuncio"
                meta_line = lines[1] if len(lines) > 1 else ""
                m_date = re.search(r"\*\*Fecha:\*\*\s*([^|]+)", meta_line)
                m_auth = re.search(r"\*\*Autor:\*\*\s*(.+)$", meta_line)
                body_txt = "\n".join(lines[2:]).replace("---", "").strip()
                anns_out.append(
                    {
                        "id": f"ann-{idx}",
                        "title": atitle,
                        "publishAt": m_date.group(1).strip() if m_date else "",
                        "author": m_auth.group(1).strip() if m_auth else "Docente",
                        "descriptionText": body_txt,
                        "descriptionHtml": f"<div style='white-space:pre-wrap'>{html.escape(body_txt)}</div>",
                    }
                )
        except Exception:
            pass

    # 4. Local files & Google Drive files
    drive_files = db.list_gdrive_files(cdir.name) if db else []
    drive_by_name = {df["name"]: df for df in drive_files}

    # Enrich weeks contents with Google Drive files that aren't stored locally in Git
    existing_week_map = {w["weekNumber"]: w for w in weeks_list}
    for df in drive_files:
        fp = df.get("folder_path", "")
        m_wk = re.search(r"Semana_(\d+)", fp)
        if m_wk:
            w_num = int(m_wk.group(1))
            w_obj = existing_week_map.get(w_num)
            if not w_obj:
                w_obj = {
                    "weekNumber": w_num,
                    "label": f"Semana {w_num:02d}",
                    "themes": [{"themeId": f"sem-{w_num}", "name": f"Materiales Semana {w_num:02d} (Google Drive)", "weekNumber": w_num, "contents": []}],
                }
                weeks_list.append(w_obj)
                existing_week_map[w_num] = w_obj
            already_in_week = any(
                c.get("filename") == df["name"] or c.get("title") == df["name"]
                for th in w_obj["themes"]
                for c in th["contents"]
            )
            if not already_in_week and w_obj["themes"]:
                w_obj["themes"][0]["contents"].append(
                    {
                        "contentId": df["file_id"],
                        "title": f"{df['name']} (Cloud Drive)",
                        "type": "DRIVE",
                        "url": df["web_view_link"],
                        "filename": df["name"],
                        "html": None,
                    }
                )
    weeks_list.sort(key=lambda x: x["weekNumber"])

    local_files: List[Dict[str, Any]] = []
    seen_names = set()
    if cdir.exists():
        for p in sorted(cdir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(DOWNLOAD_DIR).as_posix()
                seen_names.add(p.name)
                df_match = drive_by_name.get(p.name)
                local_files.append(
                    {
                        "name": p.name,
                        "rel_path": rel,
                        "drive_url": df_match["web_view_link"] if df_match else "",
                        "folder": p.parent.relative_to(cdir).as_posix() if p.parent != cdir else "Raíz del curso",
                        "size_kb": round(p.stat().st_size / 1024, 1),
                        "ext": p.suffix.lower(),
                        "source": "local+drive" if df_match else "local",
                    }
                )
    for df in drive_files:
        if df["name"] not in seen_names:
            local_files.append(
                {
                    "name": df["name"],
                    "rel_path": "",
                    "drive_url": df["web_view_link"],
                    "folder": df.get("folder_path", "Drive"),
                    "size_kb": df.get("size_kb", 0),
                    "ext": df.get("mime_type", ""),
                    "source": "gdrive",
                }
            )

    syl_path = cdir / "00_Silabo.pdf"
    syl_df = drive_by_name.get("00_Silabo.pdf")
    syl_url = (
        f"/files/{syl_path.relative_to(DOWNLOAD_DIR).as_posix()}"
        if syl_path.exists()
        else (syl_df["web_view_link"] if syl_df else None)
    )

    return {
        "courseId": class_num or course_name,
        "sectionId": class_num or course_name,
        "currentWeek": current_week,
        "numWeeks": num_weeks,
        "syllabusUrl": syl_url,
        "weeks": weeks_list,
        "tasks": tasks_out,
        "announcements": anns_out,
        "localFolder": str(cdir),
        "localFiles": local_files,
        "driveFiles": drive_files,
        "source": "local+drive",
    }


# ---------------------------------------------------------------------------
# 2. Google Drive Folder Probe & Sync (Public Link / API Key / Service Account)
# ---------------------------------------------------------------------------

def check_gdrive_status(db: StateDB) -> Dict[str, Any]:
    settings = db.get_all_settings()
    folder_id = extract_folder_id(settings.get("gdrive_folder_id", DEFAULT_GDRIVE_FOLDER_ID))
    folder_url = get_folder_url(folder_id)
    api_key = settings.get("gdrive_api_key", "").strip()
    sa_json = settings.get("gdrive_service_account_json", "").strip()

    indexed_files = db.list_gdrive_files()
    local_files_count = sum(1 for p in DOWNLOAD_DIR.rglob("*") if p.is_file()) if DOWNLOAD_DIR.exists() else 0

    mode = "public_link"
    if sa_json:
        mode = "service_account"
    elif api_key:
        mode = "api_key"

    # Quick probe of embeddedfolderview to see if the folder is publicly shared
    public_accessible = False
    probe_msg = ""
    try:
        r = requests.get(
            f"https://drive.google.com/embeddedfolderview?id={folder_id}#list",
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200 and "flip-entry" in r.text:
            public_accessible = True
            probe_msg = "Carpeta pública detectada y accesible en vivo"
        elif r.status_code == 200:
            public_accessible = True
            probe_msg = "Carpeta pública accesible (vacía o lista para sincronizar)"
        elif r.status_code in (401, 403):
            probe_msg = "Carpeta privada en Drive (compártela como 'Cualquier persona con el enlace' o usa API Key / Service Account)"
        else:
            probe_msg = f"Estado HTTP {r.status_code}"
    except Exception as exc:
        probe_msg = f"Sin conexión directa: {exc}"

    return {
        "folder_id": folder_id,
        "folder_url": folder_url,
        "embed_url": f"https://drive.google.com/embeddedfolderview?id={folder_id}#list",
        "mode": mode,
        "has_api_key": bool(api_key),
        "has_service_account": bool(sa_json),
        "public_accessible": public_accessible,
        "status_message": probe_msg,
        "indexed_drive_files": len(indexed_files),
        "local_files_count": local_files_count,
        "last_sync": settings.get("gdrive_last_sync", "Nunca"),
    }


def _parse_embedded_folder_html(html_text: str) -> List[Dict[str, str]]:
    """
    Parses Google Drive's /embeddedfolderview?id=<FOLDER_ID>#list HTML
    to extract child folders and files without needing an API key.
    """
    items: List[Dict[str, str]] = []
    entry_blocks = re.findall(
        r'<div class="flip-entry" id="entry-([a-zA-Z0-9_-]+)"[^>]*>(.*?)</div>\s*</div>\s*</div>',
        html_text,
        re.S,
    )
    if not entry_blocks:
        # Alternative simpler regex fallback
        for m in re.finditer(
            r'id="entry-([a-zA-Z0-9_-]+)".*?href="([^"]+)".*?class="flip-entry-title">([^<]+)<',
            html_text,
            re.S,
        ):
            fid, href, title = m.group(1), html.unescape(m.group(2)), html.unescape(m.group(3)).strip()
            is_folder = "drive.google.com/drive/folders/" in href or "folderview" in href
            items.append(
                {
                    "id": fid,
                    "name": title,
                    "type": "folder" if is_folder else "file",
                    "url": href,
                }
            )
        return items

    for fid, block in entry_blocks:
        href_m = re.search(r'href="([^"]+)"', block)
        title_m = re.search(r'class="flip-entry-title">([^<]+)<', block)
        href = html.unescape(href_m.group(1)) if href_m else ""
        title = html.unescape(title_m.group(1)).strip() if title_m else fid
        is_folder = "folders/" in href or "folder" in block.lower()
        items.append(
            {
                "id": fid,
                "name": title,
                "type": "folder" if is_folder else "file",
                "url": href,
            }
        )
    return items


def sync_from_gdrive(
    db: StateDB,
    download_binaries: bool = True,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Synchronizes the configured Google Drive folder (1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx)
    into local DOWNLOAD_DIR and updates the gdrive_files SQLite index.
    Supports both Google Drive API v3 (if API Key is configured) and Public Folder Crawler.
    """
    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    ensure_dirs()
    settings = db.get_all_settings()
    root_id = extract_folder_id(settings.get("gdrive_folder_id", DEFAULT_GDRIVE_FOLDER_ID))
    api_key = settings.get("gdrive_api_key", "").strip()

    log(f"Conectando a Google Drive (Carpeta ID: {root_id})...")
    discovered_files: List[Dict[str, Any]] = []
    downloaded_count = 0

    http = requests.Session()
    http.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    if api_key:
        log("Usando Google Drive API v3 con API Key...")
        downloaded_count = _crawl_with_api_key(
            http, root_id, api_key, DOWNLOAD_DIR, "", "", discovered_files, download_binaries, log
        )
    else:
        log("Escaneando vista pública de Google Drive...")
        resp = http.get(f"https://drive.google.com/embeddedfolderview?id={root_id}#list", timeout=15)
        if resp.status_code in (401, 403):
            # Even if Drive is private right now, index local files so the user sees what will be synced
            log(
                "AVISO: La carpeta de Google Drive aún está privada (HTTP 401). "
                "En Google Drive, haz clic derecho en la carpeta -> Compartir -> 'Cualquier persona con el enlace (Lector)'."
            )
            local_indexed = _index_local_files_as_drive_mirror(root_id)
            db.replace_gdrive_files(local_indexed)
            return {
                "ok": False,
                "error": "Carpeta privada en Google Drive. Cámbiala a 'Cualquier persona con el enlace' o agrega un API Key en Admin.",
                "files_indexed": len(local_indexed),
                "files_downloaded": 0,
            }
        resp.raise_for_status()
        downloaded_count = _crawl_public_folder(
            http, root_id, DOWNLOAD_DIR, "", "", discovered_files, download_binaries, log, depth=0
        )

    if not discovered_files:
        # Mirror existing local downloads into the Drive index so UI shows all files ready for Drive
        discovered_files = _index_local_files_as_drive_mirror(root_id)
        log(f"Carpeta en Drive sin archivos nuevos; {len(discovered_files)} archivos locales listos/indexados.")

    db.replace_gdrive_files(discovered_files)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.set_setting("gdrive_last_sync", now_str)
    log(f"¡Sincronización Drive completada! ({len(discovered_files)} archivos indexados, {downloaded_count} descargados)")
    return {
        "ok": True,
        "files_indexed": len(discovered_files),
        "files_downloaded": downloaded_count,
        "last_sync": now_str,
    }


def _crawl_public_folder(
    http: requests.Session,
    folder_id: str,
    local_dir: Path,
    rel_prefix: str,
    course_folder: str,
    out_files: List[Dict[str, Any]],
    download_binaries: bool,
    log: Callable[[str], None],
    depth: int = 0,
) -> int:
    if depth > 4:
        return 0
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    try:
        r = http.get(url, timeout=15)
        if r.status_code != 200:
            return 0
    except Exception:
        return 0

    entries = _parse_embedded_folder_html(r.text)
    downloaded = 0
    for item in entries:
        fid = item["id"]
        name = item["name"]
        if item["type"] == "folder":
            next_course = course_folder if course_folder else name
            next_rel = f"{rel_prefix}/{name}".strip("/")
            log(f"📁 Explorando carpeta Drive: {next_rel}")
            downloaded += _crawl_public_folder(
                http,
                fid,
                local_dir / name,
                next_rel,
                next_course,
                out_files,
                download_binaries,
                log,
                depth + 1,
            )
        else:
            dl_url = f"https://drive.google.com/uc?export=download&id={fid}"
            view_url = f"https://drive.google.com/file/d/{fid}/view"
            dest = local_dir / name
            size_kb = round(dest.stat().st_size / 1024, 1) if dest.exists() else 0.0

            if download_binaries and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with http.get(dl_url, stream=True, timeout=25) as dresp:
                        if dresp.status_code == 200:
                            with open(dest, "wb") as f:
                                for chunk in dresp.iter_content(chunk_size=65536):
                                    if chunk:
                                        f.write(chunk)
                            size_kb = round(dest.stat().st_size / 1024, 1)
                            downloaded += 1
                            log(f"⬇️ Descargado de Drive: {name} ({size_kb} KB)")
                except Exception as exc:
                    log(f"Aviso descargando {name}: {exc}")

            out_files.append(
                {
                    "file_id": fid,
                    "name": name,
                    "folder_path": rel_prefix or "Raíz",
                    "course_folder": course_folder or "Raíz Drive",
                    "mime_type": dest.suffix.lower(),
                    "size_kb": size_kb,
                    "web_view_link": view_url,
                    "download_link": dl_url,
                }
            )
    return downloaded


def _crawl_with_api_key(
    http: requests.Session,
    folder_id: str,
    api_key: str,
    local_dir: Path,
    rel_prefix: str,
    course_folder: str,
    out_files: List[Dict[str, Any]],
    download_binaries: bool,
    log: Callable[[str], None],
    depth: int = 0,
) -> int:
    if depth > 4:
        return 0
    api_url = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q": f"'{folder_id}' in parents and trashed = false",
        "fields": "files(id,name,mimeType,size,webViewLink,webContentLink)",
        "pageSize": 1000,
        "key": api_key,
    }
    resp = http.get(api_url, params=params, timeout=20)
    resp.raise_for_status()
    files = resp.json().get("files", [])
    downloaded = 0

    for item in files:
        fid = item["id"]
        name = item["name"]
        mime = item.get("mimeType", "")
        if mime == "application/vnd.google-apps.folder":
            next_course = course_folder if course_folder else name
            next_rel = f"{rel_prefix}/{name}".strip("/")
            log(f"📁 Carpeta Drive: {next_rel}")
            downloaded += _crawl_with_api_key(
                http,
                fid,
                api_key,
                local_dir / name,
                next_rel,
                next_course,
                out_files,
                download_binaries,
                log,
                depth + 1,
            )
        else:
            dest = local_dir / name
            size_kb = round(int(item.get("size", 0)) / 1024, 1)
            dl_url = item.get("webContentLink") or f"https://www.googleapis.com/drive/v3/files/{fid}?alt=media&key={api_key}"
            view_url = item.get("webViewLink") or f"https://drive.google.com/file/d/{fid}/view"

            if download_binaries and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with http.get(dl_url, stream=True, timeout=25) as dresp:
                        if dresp.status_code == 200:
                            with open(dest, "wb") as f:
                                for chunk in dresp.iter_content(chunk_size=65536):
                                    if chunk:
                                        f.write(chunk)
                            downloaded += 1
                            log(f"⬇️ Descargado de Drive: {name}")
                except Exception:
                    pass

            out_files.append(
                {
                    "file_id": fid,
                    "name": name,
                    "folder_path": rel_prefix or "Raíz",
                    "course_folder": course_folder or "Raíz Drive",
                    "mime_type": mime,
                    "size_kb": size_kb,
                    "web_view_link": view_url,
                    "download_link": dl_url,
                }
            )
    return downloaded


def _index_local_files_as_drive_mirror(root_folder_id: str) -> List[Dict[str, Any]]:
    """
    Indexes all local files in DOWNLOAD_DIR with links to the configured Google Drive folder
    so the catalog works immediately before/after the user uploads their `downloads` folder to Drive.
    """
    items: List[Dict[str, Any]] = []
    if not DOWNLOAD_DIR.exists():
        return items
    folder_link = get_folder_url(root_folder_id)
    for p in sorted(DOWNLOAD_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(DOWNLOAD_DIR).as_posix()
        parts = rel.split("/")
        course_folder = parts[0] if len(parts) > 1 else "Raíz Drive"
        folder_path = "/".join(parts[:-1]) if len(parts) > 1 else "Raíz"
        items.append(
            {
                "file_id": f"local:{rel}",
                "name": p.name,
                "folder_path": folder_path,
                "course_folder": course_folder,
                "mime_type": p.suffix.lower(),
                "size_kb": round(p.stat().st_size / 1024, 1),
                "web_view_link": folder_link,
                "download_link": f"/files/{rel}",
            }
        )
    return items
