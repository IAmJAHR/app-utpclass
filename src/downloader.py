import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests
from rich.console import Console

from .api_client import UTPClassClient
from .config import DATA_DIR, DOWNLOAD_DIR, ensure_dirs
from .notifier import notify
from .storage import StateDB

console = Console()

EMBEDDED_FILE_RE = re.compile(
    r'href=["\'](https?://[^"\']+(?:s3\.amazonaws\.com|\.pdf|\.pptx?|\.docx?|\.xlsx?|\.zip|\.rar)[^"\']*)["\']',
    re.IGNORECASE,
)
HEAVY_MEDIA_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".mp3", ".wav")


def sanitize_name(name: str, max_len: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "sin_nombre")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "sin_nombre"
    return cleaned[:max_len].strip(" .")


def strip_html(raw_html: Optional[str]) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"</p>|</li>|</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def download_binary(
    http: requests.Session,
    db: StateDB,
    file_key: str,
    url: str,
    dest_path: Path,
    etag: Optional[str] = None,
) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    if db.is_file_downloaded(file_key, etag=etag):
        return False

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with http.get(url, stream=True, timeout=(8, 20)) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        db.mark_file_downloaded(file_key, url, str(dest_path), etag=etag)
        console.print(f"     [dim]↓ {dest_path.parent.name}/{dest_path.name}[/dim]")
        return True
    except Exception as exc:
        console.print(f"[yellow]Aviso descargando {dest_path.name}: {exc}[/yellow]")
        return False


def filename_from_url(url: str, fallback: str = "archivo") -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)
    return sanitize_name(name or fallback, max_len=90)


def sync_course(
    client: UTPClassClient,
    db: StateDB,
    course: Dict[str, Any],
    notify_new: bool = True,
    include_media: bool = False,
) -> Dict[str, int]:
    stats = {"files_downloaded": 0, "new_themes": 0, "new_announcements": 0}
    course_id = course["courseId"]
    section_id = course["sectionId"]
    course_name = course.get("name", "Curso")
    class_num = course.get("classNumber", "")
    course_dir = DOWNLOAD_DIR / sanitize_name(f"{course_name} ({class_num})" if class_num else course_name, 70)
    course_dir.mkdir(parents=True, exist_ok=True)

    # 1. Syllabus
    try:
        syl = client.get_course_syllabus(course_id, section_id)
        syl_url = syl.get("syllabusUrl") if isinstance(syl, dict) else None
        if syl_url:
            if download_binary(
                client.http,
                db,
                f"syllabus:{course_id}:{section_id}",
                syl_url,
                course_dir / "00_Silabo.pdf",
            ):
                stats["files_downloaded"] += 1
    except Exception:
        pass

    # 2. Full course content (unities -> themes -> contents)
    full = client.get_course_full(course_id, section_id)
    unities = full.get("unities", [])
    summary_lines: List[str] = [
        f"# {course_name}",
        f"- **Código de sección:** `{course.get('sectionCode', '')}` | **Clase:** `{class_num}`",
        f"- **Docente:** {course.get('teacherFirstName', '')} {course.get('teacherLastName', '')} ({course.get('teacherEmail', '')})",
        f"- **Progreso:** {course.get('progress', 0)}%",
        f"- **Semana actual:** {full.get('currentWeek', '-')}/{full.get('numWeeks', '-')}",
        "",
        "## Temario y Materiales",
    ]

    for unity in unities:
        themes = unity.get("themes", [])
        for theme in themes:
            theme_id = theme.get("themeId", "")
            theme_name = theme.get("name", "Tema")
            week_num = theme.get("weekNumber")
            is_visible = theme.get("isVisible", True)
            if not is_visible:
                continue

            if theme_id and not db.is_theme_seen(theme_id):
                db.mark_theme_seen(theme_id, course_name, week_num, theme_name)
                if notify_new and week_num and week_num > 0:
                    stats["new_themes"] += 1

            week_folder = f"Semana_{int(week_num):02d}" if week_num is not None and int(week_num) > 0 else "00_Informacion_General"
            theme_dir = course_dir / week_folder / sanitize_name(theme_name, 65)
            contents = theme.get("contents", [])
            if contents:
                summary_lines.append(f"\n### {week_folder} - {theme_name}")

            url_links: List[str] = []
            for item in contents:
                if not item.get("isVisible", True):
                    continue
                cid = item.get("contentId", "")
                ctype = item.get("type", "")
                title = item.get("title", "Contenido")
                etag = item.get("etag")
                meta = item.get("metadata") or {}
                summary_lines.append(f"- [{ctype}] {title}")

                if ctype == "FILE":
                    file_url = meta.get("url")
                    fname = sanitize_name(meta.get("filename") or filename_from_url(file_url or "", title), 85)
                    if file_url:
                        if not include_media and fname.lower().endswith(HEAVY_MEDIA_EXTS):
                            url_links.append(f"- [{title} ({fname})]({file_url})")
                        elif download_binary(
                            client.http, db, f"file:{cid}", file_url, theme_dir / fname, etag=etag
                        ):
                            stats["files_downloaded"] += 1

                elif ctype == "HTML":
                    html_body = meta.get("contentHtml", "")
                    if html_body:
                        theme_dir.mkdir(parents=True, exist_ok=True)
                        html_path = theme_dir / f"{sanitize_name(title, 65)}.html"
                        if not html_path.exists() or not db.is_file_downloaded(f"html:{cid}", etag=etag):
                            html_path.write_text(
                                f"<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'><title>{html.escape(title)}</title></head><body><h1>{html.escape(title)}</h1>{html_body}</body></html>",
                                encoding="utf-8",
                            )
                            db.mark_file_downloaded(f"html:{cid}", "inline-html", str(html_path), etag=etag)

                        # Extract embedded downloadable links from HTML
                        for idx, match_url in enumerate(EMBEDDED_FILE_RE.findall(html_body), start=1):
                            clean_url_path = match_url.lower().split("?")[0]
                            if (
                                "youtube.com" in match_url
                                or "youtu.be" in match_url
                                or "vimeo.com" in match_url
                                or (not include_media and clean_url_path.endswith(HEAVY_MEDIA_EXTS))
                            ):
                                url_links.append(f"- [{title} (multimedia {idx})]({match_url})")
                                continue
                            fname = filename_from_url(match_url, f"recurso_{idx}")
                            if download_binary(
                                client.http, db, f"htmlfile:{cid}:{idx}", match_url, theme_dir / fname
                            ):
                                stats["files_downloaded"] += 1

                elif ctype == "URL":
                    ext_url = meta.get("url")
                    if ext_url:
                        url_links.append(f"- [{title}]({ext_url})")
                        if any(ext_url.lower().split("?")[0].endswith(ext) for ext in (".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".zip")):
                            fname = filename_from_url(ext_url, title)
                            if download_binary(client.http, db, f"urlfile:{cid}", ext_url, theme_dir / fname, etag=etag):
                                stats["files_downloaded"] += 1

            if url_links:
                theme_dir.mkdir(parents=True, exist_ok=True)
                (theme_dir / "Enlaces.md").write_text(
                    f"# Enlaces - {theme_name}\n\n" + "\n".join(url_links) + "\n",
                    encoding="utf-8",
                )

    # 3. Homework & Evaluations details
    hw_dir = course_dir / "Tareas_y_Evaluaciones"
    all_tasks: List[Tuple[str, Dict[str, Any]]] = []
    try:
        hw_data = client.get_course_homework(course_id, section_id)
        for item in hw_data.get("content", []):
            all_tasks.append(("TAREA", item))
    except Exception:
        pass
    try:
        ev_data = client.get_course_evaluations(course_id, section_id)
        for item in ev_data.get("content", []):
            all_tasks.append(("EVALUACIÓN", item))
    except Exception:
        pass

    if all_tasks:
        hw_dir.mkdir(parents=True, exist_ok=True)
        summary_lines.append("\n## Tareas y Evaluaciones")
        for kind, item in all_tasks:
            tid = item.get("id", "")
            title = item.get("title", "Actividad")
            status = item.get("statusActivityStudent", item.get("status", ""))
            v_from = item.get("visibleFrom", "")
            v_to = item.get("visibleTo", "")
            meta = item.get("metadata") or {}
            score = meta.get("score")
            top_score = meta.get("topScore")
            desc_html = item.get("description") or ""
            desc_txt = strip_html(desc_html)

            score_str = f"{score}/{top_score}" if score is not None else f"-/{top_score or '-'}"
            summary_lines.append(f"- [{kind}] **{title}** | Estado: `{status}` | Nota: `{score_str}` | Cierre: `{v_to}`")

            md_path = hw_dir / f"{sanitize_name(title, 70)}.md"
            md_content = (
                f"# {title}\n\n"
                f"- **Tipo:** {kind}\n"
                f"- **Estado:** {status}\n"
                f"- **Disponible desde:** {v_from}\n"
                f"- **Fecha límite:** {v_to}\n"
                f"- **Puntaje:** {score_str}\n\n"
                f"## Indicaciones\n\n{desc_txt or '_Sin descripción adicional._'}\n"
            )
            md_path.write_text(md_content, encoding="utf-8")

            for idx, match_url in enumerate(EMBEDDED_FILE_RE.findall(desc_html), start=1):
                fname = filename_from_url(match_url, f"adjunto_{idx}")
                if download_binary(client.http, db, f"taskfile:{tid}:{idx}", match_url, hw_dir / fname):
                    stats["files_downloaded"] += 1

    # 4. Announcements
    try:
        anns = client.get_course_announcements(course_id, section_id)
        ann_lines = [f"# Anuncios - {course_name}\n"]
        latest_new_title = ""
        for entry in anns:
            ann = entry.get("announcement") or {}
            aid = ann.get("id") or entry.get("id", "")
            title = ann.get("title", "Anuncio")
            pub = ann.get("publishAt", "")
            author = ann.get("authorName", "")
            body = strip_html(ann.get("description", ""))
            ann_lines.append(f"## {title}\n- **Fecha:** {pub} | **Autor:** {author}\n\n{body}\n\n---\n")

            if aid and not db.is_announcement_seen(aid):
                db.mark_announcement_seen(aid, course_name, title, pub)
                stats["new_announcements"] += 1
                if not latest_new_title:
                    latest_new_title = f"{title} ({pub})"
        if notify_new and stats["new_announcements"] > 0:
            notify(
                f"{stats['new_announcements']} anuncio(s) en {course_name}",
                f"Último: {latest_new_title}",
            )
        if anns:
            (course_dir / "Anuncios.md").write_text("\n".join(ann_lines), encoding="utf-8")
    except Exception:
        pass

    (course_dir / "RESUMEN_CURSO.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    return stats


def check_and_notify_tasks(
    client: UTPClassClient,
    db: StateDB,
    notify_alerts: bool = True,
) -> List[Dict[str, Any]]:
    ensure_dirs()
    pending = client.get_pending_activities()
    now = datetime.now()
    enriched: List[Dict[str, Any]] = []

    for act in pending:
        aid = act.get("activityId") or act.get("contentId", "")
        title = act.get("activityTitle", "Actividad")
        course_name = act.get("courseName", "Curso")
        act_type = act.get("type", "ACTIVITY")
        finish_at = act.get("finishAt", "")
        status = act.get("activityStatusFinal", "")

        hours_left: Optional[float] = None
        if finish_at:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(finish_at, fmt)
                    hours_left = (dt - now).total_seconds() / 3600.0
                    break
                except ValueError:
                    continue

        row = db.upsert_activity(aid, course_name, title, act_type, finish_at, status)
        if notify_alerts:
            if not row["notified_new"]:
                notify(
                    f"Nueva actividad ({act_type})",
                    f"{course_name}: {title}\nVence: {finish_at}",
                )
                db.mark_activity_notified(aid, "notified_new")

            if hours_left is not None and 0 < hours_left <= 24 and not row["notified_24h"]:
                notify(
                    f"⏰ Vence en menos de 24h ({act_type})",
                    f"{course_name}: {title}\nVence: {finish_at}",
                )
                db.mark_activity_notified(aid, "notified_24h")

            if hours_left is not None and 0 < hours_left <= 3 and not row["notified_3h"]:
                notify(
                    f"🚨 URGENTE: Vence en {hours_left:.1f}h",
                    f"{course_name}: {title}\nVence: {finish_at}",
                )
                db.mark_activity_notified(aid, "notified_3h")

        act_copy = dict(act)
        act_copy["hours_left"] = hours_left
        enriched.append(act_copy)

    enriched.sort(key=lambda x: x.get("finishAt") or "9999-99-99")

    # Write global AGENDA_PENDIENTES.md
    agenda_lines = [
        f"# Agenda de Actividades Pendientes (Actualizado: {now.strftime('%Y-%m-%d %H:%M')})\n",
        "| Urgencia | Tipo | Curso | Actividad | Semana | Vence | Puntaje |",
        "|---|---|---|---|---|---|---|",
    ]
    for act in enriched:
        hl = act.get("hours_left")
        if hl is None:
            urg = "⚪ Sin fecha"
        elif hl <= 0:
            urg = "⚫ Vencida"
        elif hl <= 24:
            urg = f"🔴 {hl:.1f}h"
        elif hl <= 72:
            urg = f"🟡 {hl/24:.1f}d"
        else:
            urg = f"🟢 {hl/24:.1f}d"
        agenda_lines.append(
            f"| {urg} | {act.get('type')} | {act.get('courseName')} | "
            f"{act.get('activityTitle')} | S{act.get('weekNumber', '-')} | "
            f"{act.get('finishAt', '-')} | {act.get('evaluationTopScore', '-')} pts |"
        )
    (DOWNLOAD_DIR / "AGENDA_PENDIENTES.md").write_text("\n".join(agenda_lines) + "\n", encoding="utf-8")
    return enriched
