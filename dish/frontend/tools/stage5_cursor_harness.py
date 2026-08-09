from __future__ import annotations

import base64
import re
from pathlib import Path


MODULE_IMPORT_RE = re.compile(
    r'(?P<prefix>\bfrom\s+|\bimport\s+)(?P<quote>["\'])(?P<spec>\.[^"\']+)(?P=quote)'
)


def _module_data_url(entry: Path) -> str:
    cache: dict[Path, str] = {}
    visiting: set[Path] = set()

    def build(path: Path) -> str:
        resolved = path.resolve()
        if resolved in cache:
            return cache[resolved]
        if resolved in visiting:
            raise RuntimeError(f"frontend module cycle through {resolved}")
        visiting.add(resolved)
        source = resolved.read_text(encoding="utf-8")

        def replace_import(match: re.Match[str]) -> str:
            dependency = (resolved.parent / match.group("spec")).resolve()
            if not dependency.suffix:
                dependency = dependency.with_suffix(".js")
            quote = match.group("quote")
            return f'{match.group("prefix")}{quote}{build(dependency)}{quote}'

        source = MODULE_IMPORT_RE.sub(replace_import, source)
        visiting.remove(resolved)
        encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
        result = f"data:text/javascript;base64,{encoded}"
        cache[resolved] = result
        return result

    return build(entry)


def _prepare_cursor_page(browser, src: Path, *, error_code: str, error_status: int, replacement_cursor: str | None = None):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.set_content("<body></body>")
    module_url = _module_data_url(src / "js" / "local" / "local-board-app.js")
    page.add_script_tag(
        type="module",
        content=(
            f'import {{ renderLocalPostgresqlBoard }} from "{module_url}";'
            "window.__dishRenderLocalBoard = renderLocalPostgresqlBoard;"
        ),
    )
    page.wait_for_function("typeof window.__dishRenderLocalBoard === 'function'")
    section_a = f'r1s-{"a" * 27}'
    section_b = f'r1s-{"b" * 27}'
    task_a = f'r1t-{"a" * 27}'
    task_b = f'r1t-{"b" * 27}'
    task_c = f'r1t-{"c" * 27}'
    page.evaluate(
        """async config => {
          const root = document.createElement("main"); root.id = "app"; root.className = "app-root"; document.body.append(root);
          const state = { boardCalls: 0, continuationCalls: 0, boardCursorOverride: null }; window.__dishCursorScenario = state;
          const card = (id, title, sectionId) => ({ task_id: id, title, section_id: sectionId, workflow_status: { state: "no_active_operation" }, attention_codes: [] });
          const boardPayload = () => { state.boardCalls += 1; const cursorA = state.boardCursorOverride ?? (state.boardCalls > 1 && config.autoReplacement ? config.replacementCursor : "c1.same"); return {
            snapshot_id: `d1-snapshot-${state.boardCalls}`, page_size: 1, notices: [], sections: [
              { section_id: config.sectionA, section_label: "Section A", continuity_id: "d1-a", cards: [card(config.taskA, "Task A", config.sectionA)], next_cursor: cursorA },
              { section_id: config.sectionB, section_label: "Section B", continuity_id: "d1-b", cards: [card(config.taskB, "Task B", config.sectionB)], next_cursor: "c1.other" },
            ],
          }; };
          const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json", "X-Dish-Frontend-Contract": "dish-frontend-v1" } });
          const fetchImpl = async (url) => {
            if (url === "/frontend/board") return response(boardPayload());
            if (!url.startsWith(`/frontend/sections/${config.sectionA}/tasks`)) throw new Error(`Unexpected frontend request ${url}`);
            state.continuationCalls += 1; const cursor = new URL(url, "https://dish.invalid").searchParams.get("cursor");
            if (cursor === "c1.replacement") return response({ section_id: config.sectionA, continuity_id: "d1-a", cards: [card(config.taskC, "Task C", config.sectionA)], next_cursor: null, notices: [] });
            return response({ error: { code: config.errorCode, message: "Rejected cursor." } }, config.errorStatus);
          };
          window.__dishCursorControl = await window.__dishRenderLocalBoard(root, { fetchImpl, refreshIntervalMs: 30000, setTimer: () => 1, clearTimer: () => {} });
        }""",
        {"sectionA": section_a, "sectionB": section_b, "taskA": task_a, "taskB": task_b, "taskC": task_c,
         "errorCode": error_code, "errorStatus": error_status, "replacementCursor": replacement_cursor, "autoReplacement": replacement_cursor is not None},
    )
    return page, section_a, section_b


def assert_stage5_repeated_invalid_cursors(browser, src: Path) -> None:
    for error_code, error_status in (("cursor_invalid", 400), ("cursor_stale", 409), ("request_invalid", 422)):
        page, section_a, section_b = _prepare_cursor_page(
            browser, src, error_code=error_code, error_status=error_status,
        )
        affected = page.locator(f'[data-section-id="{section_a}"]')
        unaffected = page.locator(f'[data-section-id="{section_b}"]')
        affected.get_by_role("button", name="Load more").click()
        page.wait_for_function("window.__dishCursorScenario.boardCalls === 2")
        affected.get_by_role("button", name="Load more").wait_for()
        assert unaffected.get_by_role("button", name="Load more").is_enabled()
        affected.get_by_role("button", name="Load more").click()
        affected.get_by_role("button", name="Reload required").wait_for()
        assert affected.get_by_role("button", name="Reload required").is_disabled()
        assert page.get_by_role("button", name="Reload page").is_visible()
        assert unaffected.get_by_role("button", name="Load more").is_enabled()
        assert page.evaluate("window.__dishCursorScenario.continuationCalls") == 2

        if error_code in {"cursor_invalid", "cursor_stale"}:
            page.evaluate("window.__dishCursorControl.refresh()")
            page.wait_for_function("window.__dishCursorScenario.boardCalls === 3")
            affected.get_by_role("button", name="Reload required").wait_for()
            assert affected.get_by_role("button", name="Reload required").is_disabled()
            assert page.get_by_role("button", name="Reload page").is_visible()
            assert unaffected.get_by_role("button", name="Load more").is_enabled()

            page.evaluate("window.__dishCursorScenario.boardCursorOverride = 'c1.replacement'")
            page.evaluate("window.__dishCursorControl.refresh()")
            page.wait_for_function("window.__dishCursorScenario.boardCalls === 4")
            affected.get_by_role("button", name="Load more").wait_for()
            assert affected.get_by_role("button", name="Load more").is_enabled()
            assert page.get_by_role("button", name="Reload page").count() == 0
            assert unaffected.get_by_role("button", name="Load more").is_enabled()
            affected.get_by_role("button", name="Load more").click()
            page.get_by_text("Task C", exact=True).wait_for()
            assert page.evaluate("window.__dishCursorScenario.continuationCalls") == 3

        page.evaluate("window.__dishCursorControl.stop()")
        page.close()

    page, section_a, section_b = _prepare_cursor_page(
        browser, src, error_code="cursor_invalid", error_status=400, replacement_cursor="c1.replacement",
    )
    affected = page.locator(f'[data-section-id="{section_a}"]')
    unaffected = page.locator(f'[data-section-id="{section_b}"]')
    affected.get_by_role("button", name="Load more").click()
    page.wait_for_function("window.__dishCursorScenario.boardCalls === 2")
    affected.get_by_role("button", name="Load more").wait_for()
    affected.get_by_role("button", name="Load more").click()
    page.get_by_text("Task C", exact=True).wait_for()
    assert affected.get_by_role("button", name="Reload required").count() == 0
    assert page.get_by_role("button", name="Reload page").count() == 0
    assert unaffected.get_by_role("button", name="Load more").is_enabled()
    assert page.evaluate("window.__dishCursorScenario.continuationCalls") == 2
    page.evaluate("window.__dishCursorControl.stop()")
    page.close()
