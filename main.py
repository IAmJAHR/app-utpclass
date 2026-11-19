import argparse
import sys
import time
from datetime import datetime

from rich.console import Console
from rich.table import Table

from src.api_client import UTPClassClient
from src.config import CHECK_INTERVAL_MINUTES, DOWNLOAD_DIR
from src.downloader import check_and_notify_tasks, sync_course
from src.storage import StateDB

console = Console()


def render_tasks_table(tasks):
    table = Table(title="Actividades Pendientes - UTP+class")
    table.add_column("Urgencia", style="bold")
    table.add_column("Tipo")
    table.add_column("Curso", style="cyan")
    table.add_column("Actividad")
    table.add_column("Sem", justify="center")
    table.add_column("Vence", style="magenta")

    for act in tasks:
        hl = act.get("hours_left")
        if hl is None:
            urg = "⚪ -"
        elif hl <= 0:
            urg = "[dim]⚫ Vencida[/dim]"
        elif hl <= 24:
            urg = f"[red]🔴 {hl:.1f}h[/red]"
        elif hl <= 72:
            urg = f"[yellow]🟡 {hl/24:.1f}d[/yellow]"
        else:
            urg = f"[green]🟢 {hl/24:.1f}d[/green]"

        table.add_row(
            urg,
            act.get("type", ""),
            act.get("courseName", ""),
            act.get("activityTitle", ""),
            f"S{act.get('weekNumber', '-')}",
            (act.get("finishAt") or "-")[:16],
        )
    console.print(table)


def cmd_courses(client: UTPClassClient, all_cycles: bool = False) -> None:
    courses = client.get_dashboard_courses(only_current=not all_cycles)
    table = Table(title="Cursos Activos - UTP+class")
    table.add_column("Clase", style="dim")
    table.add_column("Curso", style="cyan")
    table.add_column("Docente")
    table.add_column("Progreso", justify="right")

    for c in courses:
        first = c.get("teacherFirstName") or ""
        last = c.get("teacherLastName") or ""
        teacher = f"{first} {last}".strip()
        table.add_row(
            c.get("classNumber", ""),
            c.get("name", ""),
            teacher or "Por asignar",
            f"{c.get('progress', 0)}%",
        )
    console.print(table)


def cmd_tasks(client: UTPClassClient, db: StateDB, notify_alerts: bool = True) -> None:
    tasks = check_and_notify_tasks(client, db, notify_alerts=notify_alerts)
    render_tasks_table(tasks)
    console.print(f"[dim]Agenda guardada en: {DOWNLOAD_DIR / 'AGENDA_PENDIENTES.md'}[/dim]")


def cmd_sync(
    client: UTPClassClient,
    db: StateDB,
    notify_new: bool = True,
    all_cycles: bool = False,
    include_media: bool = False,
    course_filter: str = "",
) -> None:
    courses = client.get_dashboard_courses(only_current=not all_cycles)
    if course_filter:
        q = course_filter.lower()
        courses = [
            c
            for c in courses
            if q in (c.get("name") or "").lower() or q in (c.get("classNumber") or "").lower()
        ]
    console.print(f"[bold green]Sincronizando {len(courses)} curso(s)...[/bold green]")
    total_files = 0
    total_themes = 0
    total_anns = 0

    for c in courses:
        cname = c.get("name", "Curso")
        console.print(f"  -> [cyan]{cname}[/cyan] ...")
        stats = sync_course(
            client, db, c, notify_new=notify_new, include_media=include_media
        )
        total_files += stats["files_downloaded"]
        total_themes += stats["new_themes"]
        total_anns += stats["new_announcements"]
        console.print(
            f"     [dim]+{stats['files_downloaded']} archivo(s), "
            f"+{stats['new_themes']} tema(s), "
            f"+{stats['new_announcements']} anuncio(s)[/dim]"
        )

    console.print(
        f"[bold green]Descarga completa:[/bold green] "
        f"{total_files} archivo(s) nuevo(s), "
        f"{total_themes} tema(s) nuevo(s), "
        f"{total_anns} anuncio(s) nuevo(s)."
    )
    console.print(f"[dim]Archivos en: {DOWNLOAD_DIR}[/dim]")
    cmd_tasks(client, db, notify_alerts=notify_new)


def cmd_watch(interval_minutes: int) -> None:
    console.print(
        f"[bold cyan]Modo vigilancia activado[/bold cyan] "
        f"(revisión cada {interval_minutes} minutos). Presiona Ctrl+C para detener."
    )
    while True:
        db = StateDB()
        try:
            console.print(f"\n[dim]--- Revisión {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---[/dim]")
            client = UTPClassClient()
            cmd_sync(client, db, notify_new=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            console.print(f"[red]Error en ciclo de vigilancia: {exc}[/red]")
        finally:
            db.close()

        try:
            time.sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            console.print("\n[yellow]Vigilancia detenida por el usuario.[/yellow]")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Sistema de descarga y alertas para UTP+class")
    parser.add_argument(
        "command",
        nargs="?",
        default="web",
        choices=["web", "sync", "tasks", "courses", "watch"],
        help="Acción a ejecutar (por defecto: web)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=CHECK_INTERVAL_MINUTES,
        help="Intervalo en minutos para el modo watch",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="No enviar notificaciones emergentes en esta ejecución",
    )
    parser.add_argument(
        "--all-cycles",
        action="store_true",
        help="Incluir cursos de ciclos pasados además del ciclo actual",
    )
    parser.add_argument(
        "--include-media",
        action="store_true",
        help="Descargar también archivos multimedia pesados (.mp4, .mp3) incrustados",
    )
    parser.add_argument(
        "--course",
        type=str,
        default="",
        help="Filtrar por nombre o número de clase del curso (ej. --course 12879)",
    )
    args = parser.parse_args()

    if args.command == "web":
        from app import main as run_web_app
        run_web_app()
        return

    if args.command == "watch":
        cmd_watch(args.interval)
        return

    db = StateDB()
    try:
        client = UTPClassClient()
        if args.command == "courses":
            cmd_courses(client, all_cycles=args.all_cycles)
        elif args.command == "tasks":
            cmd_tasks(client, db, notify_alerts=not args.quiet)
        else:
            cmd_sync(
                client,
                db,
                notify_new=not args.quiet,
                all_cycles=args.all_cycles,
                include_media=args.include_media,
                course_filter=args.course,
            )
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
