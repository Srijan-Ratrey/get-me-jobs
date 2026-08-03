"""The jobhunter CLI. Presentation only — orchestration lives in pipeline.py.

This is the one module allowed to print.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
from sqlalchemy import func, select

from . import db, export as export_module, pipeline
from .config import load_company_csv, load_profile, load_targets, settings
from .models import Company, Job

app = typer.Typer(
    add_completion=False,
    help="Discover job openings, resolve a hiring contact, score fit against your profile.",
)
console = Console()

COMPANIES_YAML = Path("companies.yaml")
PROFILE_YAML = Path("profile.yaml")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Show debug logging.")):
    _setup_logging(verbose)


def _require(path: Path, what: str) -> None:
    if not path.exists():
        console.print(f"[red]{path} not found.[/] Run [bold]jobhunter init[/] first ({what}).")
        raise typer.Exit(1)


# --------------------------------------------------------------------------- #


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing YAML files."),
) -> None:
    """Create the database and starter companies.yaml / profile.yaml."""
    db.init_db()
    console.print(f"[green]✓[/] database ready at [bold]{settings.db_url}[/]")

    # Bootstrap from the shipped *.example.yaml. Copying `companies.yaml` onto
    # itself is what this used to do when run from the repo root, which silently
    # did nothing and left a fresh clone with no config at all.
    packaged = Path(__file__).parent.parent
    for name in (COMPANIES_YAML, PROFILE_YAML):
        source = packaged / f"{name.stem}.example{name.suffix}"
        if name.exists() and not force:
            console.print(f"[dim]·[/] {name} already exists, leaving it alone")
        elif source.exists():
            shutil.copy(source, name)
            console.print(f"[green]✓[/] wrote {name} from {source.name}")
        else:
            console.print(f"[yellow]![/] {source.name} is missing; create {name} by hand")

    console.print("\nNext: [bold]jobhunter scan[/] to fetch openings.")


@app.command()
def resolve(
    from_csv: Path = typer.Option(
        ..., "--from", help="CSV of companies. Needs a name column and a careers-URL column."
    ),
    companies: Path = typer.Option(COMPANIES_YAML, "--companies", help="Targets YAML to append to."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only, write nothing."),
) -> None:
    """Fingerprint companies' careers pages to discover their ATS and board token."""
    if not from_csv.exists():
        console.print(f"[red]{from_csv} not found.[/]")
        raise typer.Exit(1)

    try:
        candidates, skipped = load_company_csv(from_csv)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    for reason in skipped:
        console.print(f"[yellow]skipped[/] {reason}")

    already = {t.name.strip().lower() for t in load_targets(companies)} if companies.exists() else set()
    todo = [t for t in candidates if t.name.strip().lower() not in already]
    console.print(
        f"{len(candidates)} in {from_csv.name} · [dim]{len(candidates) - len(todo)} already "
        f"tracked[/] · resolving [bold]{len(todo)}[/]"
        + (" [yellow](dry run)[/]" if dry_run else "")
    )
    if not todo:
        console.print("[green]Nothing new to resolve.[/]")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("fingerprinting", total=len(todo))

        def tick(name: str) -> None:
            progress.update(task, description=f"fingerprinting [cyan]{name}[/]")
            progress.advance(task)

        result = asyncio.run(
            pipeline.run_resolve(
                todo, companies_path=companies, dry_run=dry_run, on_progress=tick
            )
        )

    if result.resolved:
        table = Table(title="Resolved", header_style="bold")
        table.add_column("Company")
        table.add_column("ATS")
        table.add_column("Token")
        for outcome in sorted(result.resolved, key=lambda o: (o.ats or "", o.target.name)):
            table.add_row(outcome.target.name, outcome.ats or "?", outcome.token or "?")
        console.print(table)

    console.print(
        f"\n[green]{len(result.resolved)}[/] resolved {result.by_ats()} · "
        f"[yellow]{len(result.unsupported)}[/] unsupported ATS · "
        f"[yellow]{len(result.no_fingerprint)}[/] no fingerprint · "
        f"[red]{len(result.unreachable)}[/] unreachable"
    )

    if result.unsupported:
        # This tally is the answer to "which adapter should I write next".
        console.print(f"[dim]Missing adapters would unlock: {result.unsupported_by_ats()}[/]")

    if result.misses:
        report = Path("unresolved-companies.md")
        lines = [
            "# Companies not resolved",
            "",
            "Regenerated by `jobhunter resolve`. The ATS tally above is the backlog of which",
            "adapter to build next; see docs/sources.md.",
            "",
        ]
        for label, bucket in (
            ("Unsupported ATS", result.unsupported),
            ("No fingerprint found", result.no_fingerprint),
            ("Unreachable", result.unreachable),
        ):
            if not bucket:
                continue
            lines += [f"## {label} ({len(bucket)})", ""]
            lines += [
                f"- **{o.target.name}** — {o.detail}  \n  <{o.target.careers_url}>" for o in bucket
            ]
            lines.append("")
        if not dry_run:
            report.write_text("\n".join(lines) + "\n")
            console.print(f"[dim]Wrote {report} ({len(result.misses)} companies).[/]")

    if dry_run:
        console.print("[yellow]Dry run: companies.yaml was not touched.[/]")
    else:
        console.print(f"[green]✓[/] appended [bold]{result.added}[/] companies to {companies}")
        console.print("\nNext: [bold]jobhunter scan[/] — which also verifies the new tokens.")


@app.command()
def scan(
    companies: Path = typer.Option(COMPANIES_YAML, "--companies", help="Targets YAML."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch but write nothing."),
) -> None:
    """Fetch jobs from every target in companies.yaml."""
    _require(companies, "it lists the companies to watch")
    targets = load_targets(companies)
    if not targets:
        console.print("[yellow]No companies configured.[/]")
        raise typer.Exit(1)

    db.init_db()
    console.print(
        f"Scanning [bold]{len(targets)}[/] targets at "
        f"{settings.requests_per_second} req/sec per host"
        + (" [yellow](dry run)[/]" if dry_run else "")
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("scanning", total=len(targets))

        def tick(name: str) -> None:
            progress.update(task, description=f"scanning [cyan]{name}[/]")
            progress.advance(task)

        result = asyncio.run(pipeline.run_scan(targets, dry_run=dry_run, on_progress=tick))

    if not dry_run:
        with db.session_scope() as session:
            run = db.start_run(session)
            db.finish_run(
                session,
                run,
                jobs_seen=result.jobs_seen,
                jobs_new=result.jobs_new,
                errors=result.errors,
            )

    table = Table(title="Scan results", header_style="bold")
    table.add_column("Company")
    table.add_column("Seen", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Closed", justify="right")
    for name, stats in result.per_company.items():
        if "error" in stats:
            table.add_row(name, "[red]—[/]", "[red]—[/]", f"[red]{stats['error'][:44]}[/]")
        else:
            table.add_row(name, str(stats["seen"]), str(stats["new"]), str(stats["closed"]))
    console.print(table)
    console.print(
        f"[bold]{result.jobs_seen}[/] seen · [green]{result.jobs_new}[/] new · "
        f"{result.jobs_closed} closed · [red]{len(result.errors)}[/] failed"
    )
    if result.errors:
        console.print("[dim]Failures are recorded in runs.errors and did not stop the scan.[/]")
    console.print("\nNext: [bold]jobhunter score[/]")


@app.command()
def score(
    profile_path: Path = typer.Option(PROFILE_YAML, "--profile", help="Profile YAML."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Score but write nothing."),
) -> None:
    """Rescore every open job against profile.yaml."""
    _require(profile_path, "it defines what you are looking for")
    profile = load_profile(profile_path)
    db.init_db()

    summary = pipeline.run_score(profile, dry_run=dry_run)

    table = Table(title="Score distribution", header_style="bold")
    table.add_column("Band")
    table.add_column("Jobs", justify="right")
    for band, count in summary["buckets"].items():
        table.add_row(band, str(count))
    console.print(table)
    console.print(
        f"[bold]{summary['scored']}[/] scored · "
        f"[yellow]{summary['disqualified']}[/] hard-zeroed (excluded title or too many years)"
    )
    console.print(f"\nNext: [bold]jobhunter list --min-score {profile.min_score}[/]")


@app.command()
def contacts(
    companies: Path = typer.Option(COMPANIES_YAML, "--companies", help="Targets YAML."),
    verify: bool = typer.Option(
        False, "--verify", help="Enable SMTP verification (off by default; see docs/compliance.md)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve but write nothing."),
) -> None:
    """Resolve a hiring contact for companies with open jobs."""
    _require(companies, "it lists the companies to watch")
    targets = load_targets(companies)
    db.init_db()

    if verify:
        console.print(
            "[yellow]SMTP verification enabled.[/] Probes are serial, delayed and capped, "
            "and never issue DATA. Set a real smtp_helo_host you control."
        )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("resolving", total=len(targets))

        def tick(name: str) -> None:
            progress.update(task, description=f"resolving [cyan]{name}[/]")
            progress.advance(task)

        result = asyncio.run(
            pipeline.run_contacts(
                targets, dry_run=dry_run, verify_emails=verify or None, on_progress=tick
            )
        )

    table = Table(title="Contacts", header_style="bold")
    table.add_column("Company")
    table.add_column("Best contact")
    table.add_column("Conf", justify="right")
    table.add_column("How")
    for name, stats in result.per_company.items():
        if stats.get("found"):
            table.add_row(name, stats["best"], f"{stats['confidence']:.2f}", stats["method"])
    if table.row_count:
        console.print(table)

    console.print(
        f"[bold]{result.companies_checked}[/] companies · "
        f"[green]{result.contacts_found}[/] contacts · "
        f"{len(result.companies_without_contact)} with none"
    )
    if result.companies_without_contact:
        # An honest "no contact" is a useful answer; a guess dressed up is not.
        console.print(
            "[dim]No contact found (apply through the posting): "
            + ", ".join(result.companies_without_contact[:12])
            + "[/]"
        )
    console.print("\nNext: [bold]jobhunter export jobs.xlsx[/]")


@app.command()
def purge(
    email: str = typer.Option(..., "--email", help="Address to erase and suppress."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """GDPR erasure: delete a contact and never rediscover it."""
    db.init_db()
    if not yes:
        typer.confirm(
            f"Hard-delete every row for {email} and suppress future rediscovery?", abort=True
        )
    with db.session_scope() as session:
        deleted = db.purge_contact(session, email)
    console.print(
        f"[green]✓[/] deleted [bold]{deleted}[/] contact row(s) and suppressed {email}.\n"
        "[dim]The suppression list stores a hash, not the address, so honouring the "
        "request does not require retaining the data.[/]"
    )


@app.command(name="list")
def list_jobs(
    min_score: int = typer.Option(0, "--min-score", help="Only jobs at or above this score."),
    limit: int = typer.Option(30, "--limit", help="Rows to show."),
    company: str | None = typer.Option(None, "--company", help="Filter by company name."),
    include_closed: bool = typer.Option(False, "--include-closed"),
    show_why: bool = typer.Option(False, "--why", help="Show the score breakdown."),
    since: str | None = typer.Option(
        None, "--since", help="Only jobs first seen since: 7d, 24h, 2w, last-scan, or a date."
    ),
    posted_within: str | None = typer.Option(
        None, "--posted-within", help="Only jobs the company posted within: 7d, 30d, a date."
    ),
) -> None:
    """Show scored openings, best first."""
    db.init_db()
    try:
        since_at = pipeline.resolve_since(since)
        posted_at = pipeline.resolve_since(posted_within)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    with db.session_scope() as session:
        query = (
            select(Job, Company)
            .join(Company, Job.company_id == Company.id)
            .order_by(Job.fit_score.desc().nullslast(), Job.first_seen.desc())
        )
        if not include_closed:
            query = query.where(Job.closed_at.is_(None))
        if min_score:
            query = query.where(Job.fit_score >= min_score)
        if company:
            query = query.where(Company.name.ilike(f"%{company}%"))
        if since_at is not None:
            query = query.where(Job.first_seen >= since_at)
        if posted_at is not None:
            query = query.where(Job.posted_at >= posted_at)
        rows = session.execute(query.limit(limit)).all()

    if not rows:
        console.print("[yellow]No jobs match.[/] Try a lower --min-score, or run scan/score first.")
        return

    table = Table(header_style="bold", show_lines=show_why)
    table.add_column("Score", justify="right")
    table.add_column("Company")
    table.add_column("Title")
    table.add_column("Location")
    table.add_column("Level")
    if show_why:
        table.add_column("Why")
    for job, comp in rows:
        colour = "green" if (job.fit_score or 0) >= 70 else "yellow" if (job.fit_score or 0) >= 50 else "dim"
        cells = [
            f"[{colour}]{job.fit_score if job.fit_score is not None else '—'}[/]",
            comp.name,
            job.title[:58],
            (job.location or "—")[:24],
            job.seniority or "—",
        ]
        if show_why:
            cells.append("\n".join((job.fit_reasons or {}).get("reasons") or []))
        table.add_row(*cells)
    console.print(table)
    console.print(f"[dim]{len(rows)} shown. Add --why to see how each score was built.[/]")


@app.command()
def export(
    path: Path = typer.Argument(..., help="Output file: .xlsx or .csv"),
    min_score: int = typer.Option(0, "--min-score"),
    include_closed: bool = typer.Option(False, "--include-closed"),
    since: str | None = typer.Option(
        None, "--since", help="Only jobs first seen since: 7d, 24h, 2w, last-scan, or a date."
    ),
    posted_within: str | None = typer.Option(
        None, "--posted-within", help="Only jobs the company posted within: 7d, 30d, a date."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the row count, write nothing."),
) -> None:
    """Export one row per job with its best contact."""
    db.init_db()
    try:
        filters = {
            "since": pipeline.resolve_since(since),
            "posted_within": pipeline.resolve_since(posted_within),
        }
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    if dry_run:
        rows = export_module.collect_rows(
            min_score=min_score, include_closed=include_closed, **filters
        )
        console.print(f"[yellow](dry run)[/] would write {len(rows)} rows to {path}")
        return
    try:
        count = export_module.export(
            path, min_score=min_score, include_closed=include_closed, **filters
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(f"[green]✓[/] wrote [bold]{count}[/] rows to {path}")


@app.command()
def stats() -> None:
    """A quick look at what is in the database."""
    db.init_db()
    with db.session_scope() as session:
        companies = session.scalar(select(func.count()).select_from(Company)) or 0
        total = session.scalar(select(func.count()).select_from(Job)) or 0
        open_jobs = session.scalar(
            select(func.count()).select_from(Job).where(Job.closed_at.is_(None))
        ) or 0
        scored = session.scalar(
            select(func.count()).select_from(Job).where(Job.fit_score.is_not(None))
        ) or 0
    table = Table(header_style="bold")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for label, value in (
        ("Companies", companies),
        ("Jobs (all time)", total),
        ("Jobs open", open_jobs),
        ("Jobs scored", scored),
    ):
        table.add_row(label, str(value))
    console.print(table)


if __name__ == "__main__":
    app()
