"""Player bank desk: credit, loan offers, originate, payoff."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from engine.banking import (
    debt_and_headroom,
    get_loan_offers,
    list_loans,
    originate_loan,
    payoff_loan,
)


def _money(n: float) -> str:
    return f"${float(n or 0):,.0f}"


def _parse_amount(raw: str) -> float:
    return float(str(raw).replace(",", "").replace("$", "").strip())


def _resolve_loan_id(token: str) -> str:
    needle = str(token or "").strip()
    rows = list_loans(active_only=True)
    exact = [r for r in rows if str(r.get("loan_id")) == needle]
    if exact:
        return str(exact[0]["loan_id"])
    pref = [r for r in rows if str(r.get("loan_id") or "").startswith(needle)]
    if len(pref) == 1:
        return str(pref[0]["loan_id"])
    if len(pref) > 1:
        raise ValueError("Ambiguous loan id; use more characters.")
    raise ValueError("No active loan with that id.")


def bank_panel(console: Console, amount: float | None = None) -> None:
    snap = debt_and_headroom()
    console.print("\n[bold cyan]BANK[/bold cyan]")
    console.print(
        f"  Credit {snap['credit_score']}  ·  {snap['bracket_label']}\n"
        f"  Cash {_money(snap['cash'])}  ·  Debt {_money(snap['debt'])}  ·  "
        f"Room {_money(snap['headroom'])}\n"
        f"  Cap {_money(snap['borrowing_cap'])}  ·  Weekly service {_money(snap['weekly_service'])}\n"
        f"  Weekly revenue (cap basis) {_money(snap['weekly_revenue'])}\n"
    )
    preview = amount
    if preview is None:
        room = float(snap.get("headroom") or 0)
        preview = min(10_000_000.0, room) if room >= 100_000 else 100_000.0
    _print_offers(console, preview)
    _print_loans(console, active_only=True)


def _print_offers(console: Console, amount: float) -> None:
    try:
        offers = get_loan_offers(float(amount))
    except Exception as e:
        console.print(f"[red]Offers for {_money(amount)}: {e}[/red]\n")
        return
    t = Table(title=f"OFFERS — {_money(amount)}", show_lines=False)
    t.add_column("#", justify="right", width=3)
    t.add_column("Weeks", justify="right", width=8)
    t.add_column("APR", justify="right", width=8)
    t.add_column("Weekly PMT", justify="right", width=14)
    t.add_column("Total paid", justify="right", width=14)
    for i, o in enumerate(offers, start=1):
        t.add_row(
            str(i),
            str(o["weeks"]),
            f"{float(o['annual_rate']) * 100:.2f}%",
            _money(o["weekly_payment"]),
            _money(o["total_paid"]),
        )
    console.print(t)
    console.print("[dim]loan_take <weeks|index> <amount>   ·   loan_payoff <id>[/dim]\n")


def _print_loans(console: Console, *, active_only: bool = False) -> None:
    rows = list_loans(active_only=active_only)
    title = "ACTIVE LOANS" if active_only else "LOANS"
    t = Table(title=title, show_lines=False)
    t.add_column("Id", style="cyan", width=10)
    t.add_column("Status", width=10)
    t.add_column("Remaining", justify="right", width=14)
    t.add_column("Weekly", justify="right", width=12)
    t.add_column("Weeks left", justify="right", width=10)
    t.add_column("From wk", justify="right", width=8)
    if not rows:
        console.print("[dim]No loans.[/dim]\n")
        return
    for r in rows:
        t.add_row(
            str(r.get("loan_id") or "")[:8],
            str(r.get("status") or ""),
            _money(r.get("principal_remaining")),
            _money(r.get("weekly_payment")),
            str(r.get("weeks_remaining") or 0),
            str(r.get("originated_week") or ""),
        )
    console.print(t)
    console.print()


def handle_bank_cli(console: Console, raw: str) -> None:
    parts = raw.strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    try:
        if cmd == "bank":
            amt = _parse_amount(parts[1]) if len(parts) >= 2 else None
            bank_panel(console, amt)
            return
        if cmd == "loans":
            _print_loans(console, active_only=False)
            return
        if cmd == "loan_offers":
            if len(parts) < 2:
                console.print("[yellow]Usage:[/yellow] loan_offers <amount>\n")
                return
            _print_offers(console, _parse_amount(parts[1]))
            return
        if cmd == "loan_take":
            if len(parts) < 3:
                console.print("[yellow]Usage:[/yellow] loan_take <weeks|index> <amount>\n")
                return
            token = int(parts[1])
            amount = _parse_amount(parts[2])
            weeks = token
            if token in (1, 2, 3):
                offers = get_loan_offers(amount)
                if token > len(offers):
                    raise ValueError(f"Only {len(offers)} offer(s) for that amount.")
                weeks = int(offers[token - 1]["weeks"])
            out = originate_loan(amount, weeks)
            console.print(
                f"[green]Loan funded {_money(out['amount'])} over {out['weeks']} weeks · "
                f"weekly {_money(out['weekly_payment'])} · credit {out['credit_score']}.[/green]\n"
            )
            return
        if cmd == "loan_payoff":
            if len(parts) < 2:
                console.print("[yellow]Usage:[/yellow] loan_payoff <id>\n")
                return
            lid = _resolve_loan_id(parts[1])
            out = payoff_loan(lid)
            console.print(f"[green]Paid off {_money(out['paid'])}. Debt now {_money(out['debt'])}.[/green]\n")
            return
    except Exception as e:
        console.print(f"[red]{e}[/red]\n")
        return
    console.print(
        "[yellow]Usage:[/yellow] bank [amount] · loan_offers <amount> · "
        "loan_take <weeks|index> <amount> · loan_payoff <id> · loans\n"
    )
