"""Herdr session graph capture, merge, Markdown ledger, and wave selection."""

from __future__ import annotations

from pathlib import Path

import orjson
from pydantic import ValidationError

from dotfiles_py.time_ns import format_ns_timestamp
from herdr_roll.adapters import ADAPTERS, detect_kind
from herdr_roll.herdr import LiveClient, hc_read
from herdr_roll.models import (
    GraphSnapshot,
    HarnessKind,
    Role,
    SpaceSnapshot,
    TabSnapshot,
    TerminalSnapshot,
)

LEAD_SUFFIXES = ("-lead", "-lead-agy", "-lead-hermes", "-coord", "-shadow", "-v2")
COORDINATOR_SPACE = "chief-of-staff"
COORDINATOR_TABS = frozenset({"chief-of-staff", "cos-shadow"})


class GraphError(Exception):
    """Raised when Herdr state is missing, empty, or invalid."""


class RefusedError(Exception):
    """Raised on invalid target selectors or lock contention."""


def role_of(space_name: str, tab_name: str) -> Role:
    """Classify tab scheduling wave role cleanly by name."""
    if space_name == COORDINATOR_SPACE and tab_name in COORDINATOR_TABS:
        return Role.COORDINATOR
    if tab_name.endswith(LEAD_SUFFIXES) or space_name == COORDINATOR_SPACE:
        return Role.LEAD
    return Role.CHILD


def build_terminal(
    live: LiveClient, address: str, coordinator_addr: str | None
) -> TerminalSnapshot:
    """Inspect a single live terminal and resolve its harness kind and session ID."""
    info = live.info.get(address) or {}
    ls_row = live.terminals.get(address) or {}
    fg_pid, fg_argv = live.foreground(address)
    raw_term_argv = info.get("argv")
    term_argv = (
        [str(x) for x in raw_term_argv] if isinstance(raw_term_argv, list) else []
    )
    status_obj = info.get("status")
    status_state = (
        status_obj.get("state")
        if isinstance(status_obj, dict)
        else ls_row.get("status")
    )
    status_app = (
        status_obj.get("app") if isinstance(status_obj, dict) else ls_row.get("agent")
    )
    status_state_str = str(status_state) if isinstance(status_state, str) else None
    status_app_str = str(status_app) if isinstance(status_app, str) else None

    fg_obj = info.get("foreground")
    cwd = (
        (fg_obj.get("cwd") if isinstance(fg_obj, dict) else None)
        or info.get("cwd")
        or ls_row.get("cwd")
    )
    cwd_str = str(cwd) if isinstance(cwd, str) and cwd else None
    title = str(info.get("title") or "")
    raw_turn = info.get("turn")
    turn = (
        raw_turn
        if isinstance(raw_turn, int) and not isinstance(raw_turn, bool)
        else None
    )
    raw_root_pid = info.get("pid")
    root_pid = (
        raw_root_pid
        if isinstance(raw_root_pid, int) and not isinstance(raw_root_pid, bool)
        else None
    )

    kind = detect_kind(status_app_str, fg_argv, term_argv)
    session_id: str | None = None
    if kind is not HarnessKind.SHELL:
        adapter = ADAPTERS[kind]
        screen_text = hc_read(address, lines=120) if kind is HarnessKind.HERMES else ""
        session_id = adapter.session_id(
            address=address,
            tree_pids=live.tree(address),
            fg_argv=fg_argv,
            term_argv=term_argv,
            cwd=cwd_str,
            title=title,
            screen_text=screen_text,
        )

    return TerminalSnapshot(
        address=address,
        kind=kind,
        session_id=session_id,
        cwd=cwd_str,
        fg_argv=list(fg_argv),
        term_argv=list(term_argv),
        pid=root_pid,
        fg_pid=fg_pid,
        status_state=status_state_str,
        status_app=status_app_str,
        title=title,
        turn=turn,
        is_coordinator=(address == coordinator_addr),
    )


def _coordinator_address(live: LiveClient) -> str | None:
    for space in live.spaces:
        sname = str(space.get("name") or "")
        for tab in space.get("tabs") or ():
            if not isinstance(tab, dict):
                continue
            tname = str(tab.get("name") or "")
            if role_of(sname, tname) is Role.COORDINATOR:
                for pane in tab.get("terminals") or ():
                    if (
                        isinstance(pane, dict)
                        and (addr := pane.get("terminal"))
                        and str(addr) in live.terminals
                    ):
                        return str(addr)
    return None


def build_graph(live: LiveClient) -> GraphSnapshot:
    """Build a full GraphSnapshot from live `herdr-app ls`, `hc ls`, and `hc get` data."""
    if not live.spaces:
        raise GraphError("herdr-app ls returned no spaces; is Herdr running?")
    if not live.listed:
        raise GraphError("hc ls --json returned no terminal list; is hc running?")

    coordinator_addr = _coordinator_address(live)
    spaces: list[SpaceSnapshot] = []
    for raw_space in live.spaces:
        tabs: list[TabSnapshot] = []
        for raw_tab in raw_space.get("tabs") or ():
            if not isinstance(raw_tab, dict):
                continue
            terms: list[TerminalSnapshot] = []
            for pane in raw_tab.get("terminals") or ():
                if not isinstance(pane, dict):
                    continue
                addr = pane.get("terminal")
                if addr and str(addr) in live.terminals:
                    terms.append(build_terminal(live, str(addr), coordinator_addr))
            tabs.append(
                TabSnapshot(
                    id=str(raw_tab["id"]),
                    name=str(raw_tab["name"]),
                    terminals=terms,
                )
            )
        folder = raw_space.get("folder")
        spaces.append(
            SpaceSnapshot(
                id=str(raw_space["id"]),
                name=str(raw_space["name"]),
                folder=str(folder) if isinstance(folder, str) and folder else None,
                tabs=tabs,
            )
        )
    taken_at = format_ns_timestamp()
    return GraphSnapshot(
        taken_at=taken_at,
        coordinator=coordinator_addr,
        spaces=spaces,
    )


def merge_graph(
    current: GraphSnapshot, previous: GraphSnapshot | None
) -> GraphSnapshot:
    """Retain closed spaces and tabs from *previous* for reboot recovery without dropping active ones."""
    if previous is None:
        return current
    all_live_sessions = {
        term.session_id
        for space in current.spaces
        for tab in space.tabs
        for term in tab.terminals
        if term.session_id
    }
    by_id = {space.id: space for space in current.spaces}
    matched_ids = {space.id for space in previous.spaces if space.id in by_id}
    unmatched_prev_names: dict[str, int] = {}
    for sp in previous.spaces:
        if sp.id not in by_id:
            unmatched_prev_names[sp.name] = unmatched_prev_names.get(sp.name, 0) + 1
    by_name_unique: dict[str, list[SpaceSnapshot]] = {}
    for sp in current.spaces:
        if sp.id not in matched_ids:
            by_name_unique.setdefault(sp.name, []).append(sp)

    merged_live = {space.id: space for space in current.spaces}
    kept_spaces: list[SpaceSnapshot] = []
    for old_space in previous.spaces:
        now_space = by_id.get(old_space.id)
        if (
            now_space is None
            and unmatched_prev_names.get(old_space.name, 0) == 1
            and len(by_name_unique.get(old_space.name, ())) == 1
        ):
            now_space = by_name_unique[old_space.name][0]
        if now_space is None:
            gone_closed_tabs: list[TabSnapshot] = []
            for tab in old_space.tabs:
                kept_terms = [
                    t
                    for t in tab.terminals
                    if t.session_id and t.session_id not in all_live_sessions
                ]
                if kept_terms:
                    gone_closed_tabs.append(
                        TabSnapshot(
                            id=tab.id,
                            name=tab.name,
                            terminals=kept_terms,
                            kept_from_previous_graph=True,
                        )
                    )
            if gone_closed_tabs:
                kept_spaces.append(
                    SpaceSnapshot(
                        id=old_space.id,
                        name=old_space.name,
                        folder=old_space.folder,
                        tabs=gone_closed_tabs,
                        kept_from_previous_graph=True,
                    )
                )
            continue

        cur_merged = merged_live[now_space.id]
        live_tab_names = {tab.name for tab in cur_merged.tabs}
        gone_tabs: list[TabSnapshot] = []
        for tab in old_space.tabs:
            if tab.name in live_tab_names:
                continue
            kept_terms = [
                t
                for t in tab.terminals
                if t.session_id and t.session_id not in all_live_sessions
            ]
            if kept_terms:
                gone_tabs.append(
                    TabSnapshot(
                        id=tab.id,
                        name=tab.name,
                        terminals=kept_terms,
                        kept_from_previous_graph=True,
                    )
                )
        if gone_tabs:
            merged_live[now_space.id] = SpaceSnapshot(
                id=cur_merged.id,
                name=cur_merged.name,
                folder=cur_merged.folder,
                tabs=[*cur_merged.tabs, *gone_tabs],
                kept_from_previous_graph=cur_merged.kept_from_previous_graph,
            )
    ordered_spaces = [merged_live[s.id] for s in current.spaces] + kept_spaces
    return GraphSnapshot(
        taken_at=current.taken_at,
        coordinator=current.coordinator,
        spaces=ordered_spaces,
    )


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_ledger(graph: GraphSnapshot) -> str:
    """Render a clean Markdown summary table of all spaces/tabs/terminals."""
    lines = [
        f"# Herdr Session Graph ({graph.taken_at})",
        "",
        "| space | tab | terminal | kind | session_id | status | cwd |",
        "|---|---|---|---|---|---|---|",
    ]
    for space in graph.spaces:
        for tab in space.tabs:
            for term in tab.terminals:
                suffix = " (coordinator)" if term.is_coordinator else ""
                row = [
                    _cell(space.name),
                    _cell(tab.name),
                    _cell(term.address + suffix),
                    _cell(term.kind.value),
                    _cell(term.session_id or "-"),
                    _cell(term.status_state or "-"),
                    _cell(term.cwd or "-"),
                ]
                lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


TargetTuple = tuple[SpaceSnapshot, TabSnapshot, TerminalSnapshot]


def _tab_matches(
    selector: str, space: SpaceSnapshot, tab: TabSnapshot, term: TerminalSnapshot
) -> bool:
    if selector == term.address or selector == tab.id or selector == tab.name:
        return True
    if "/" in selector:
        sp, tb = selector.split("/", 1)
        return space.name == sp.strip() and tab.name == tb.strip()
    return False


def select_targets(
    graph: GraphSnapshot,
    *,
    tabs: tuple[str, ...] = (),
    spaces: tuple[str, ...] = (),
    harnesses: tuple[HarnessKind, ...] = (),
    all_targets: bool = False,
    exclude: tuple[str, ...] = (),
    caller_id: str | None = None,
    recover: bool = False,
) -> list[TargetTuple]:
    """Resolve and validate ordered targets `(Wave 0 coordinator -> Wave 1 children -> Wave 2 leads)`."""
    if not (tabs or spaces or harnesses or all_targets):
        raise RefusedError("Specify --tabs, --spaces, --harnesses, or --all.")

    known_spaces = {
        s.name for s in graph.spaces if recover or not s.kept_from_previous_graph
    }
    if unknown_spaces := sorted(set(spaces) - known_spaces):
        raise RefusedError(
            f"Unknown space(s): {', '.join(unknown_spaces)}; available spaces: {', '.join(sorted(known_spaces))}"
        )

    candidates: list[TargetTuple] = []
    for space in graph.spaces:
        keep_closed = recover and (not spaces or space.name in spaces)
        if space.kept_from_previous_graph and not keep_closed:
            continue
        for tab in space.tabs:
            if tab.kept_from_previous_graph and not keep_closed:
                continue
            for term in tab.terminals:
                if term.kind is HarnessKind.SHELL:
                    continue
                candidates.append((space, tab, term))

    if tabs:
        unmatched = [
            sel
            for sel in tabs
            if not any(_tab_matches(sel, sp, tb, tm) for sp, tb, tm in candidates)
        ]
        if unmatched:
            raise RefusedError(
                f"Unknown or non-agent tab/terminal selector(s): {', '.join(unmatched)}"
            )

    selected: list[TargetTuple] = []
    excluded_set = set(exclude)
    space_set = set(spaces)
    harness_set = set(harnesses)

    for space, tab, term in candidates:
        if (
            space.name in excluded_set
            or tab.name in excluded_set
            or term.address in excluded_set
        ):
            continue
        if caller_id and term.address == caller_id and term.address not in tabs:
            continue
        if spaces and space.name not in space_set:
            continue
        if harnesses and term.kind not in harness_set:
            continue
        if tabs and not any(_tab_matches(sel, space, tab, term) for sel in tabs):
            continue
        selected.append((space, tab, term))

    rank = {Role.COORDINATOR: 0, Role.CHILD: 1, Role.LEAD: 2}
    space_order = [s.name for s in graph.spaces]

    def sort_key(item: TargetTuple) -> tuple[int, int, str, str]:
        sp, tb, tm = item
        role = role_of(sp.name, tb.name)
        idx = space_order.index(sp.name) if sp.name in space_order else len(space_order)
        return (rank[role], idx, tb.name, tm.address)

    return sorted(selected, key=sort_key)


def load_graph(path: Path) -> GraphSnapshot:
    """Read and validate a saved GraphSnapshot JSON file using `orjson` + Pydantic v2."""
    try:
        raw = orjson.loads(path.read_bytes())
        return GraphSnapshot.model_validate(raw)
    except (OSError, ValueError, ValidationError) as err:
        raise GraphError(f"Unable to read saved graph at {path}: {err}") from None
