from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]
SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")


def section(start: str, end: str) -> str:
    return SOURCE[SOURCE.index(start):SOURCE.index(end, SOURCE.index(start))]


def run_behavior(scenario: str) -> None:
    subprocess.run(
        ["node", "tests/desktop_behavior.mjs", scenario],
        cwd=ROOT, check=True, text=True, capture_output=True,
    )


def test_stale_resume_after_stop_is_fenced_behaviorally():
    run_behavior("stale-resume")


def test_stop_during_pending_completion_then_resume_behaviorally():
    run_behavior("stop-during-completion")


def test_completion_failure_followed_by_reload_behaviorally():
    run_behavior("completion-reload")


def test_two_panes_reconcile_backend_completion_intents_without_lost_updates():
    run_behavior("concurrent-pane-completions")


def test_stop_without_focused_session_reaches_backend_behaviorally():
    run_behavior("stop-no-session")


def test_remote_stop_pauses_audio_fences_continuation_and_clears_switch():
    run_behavior("remote-stop")


def test_statusbar_toggle_hides_and_restores_keep_alive_reader_pane():
    run_behavior("pane-toggle")


def test_stopped_resume_response_does_not_start_playback_behaviorally():
    run_behavior("stopped-resume-response")


def test_request_more_reserves_before_first_await_and_clears_by_identity():
    body = section("  async function requestMore", "  async function loadItem")
    reserve = "generationRef.current = reservation;"
    assert body.index(reserve) < body.index("await ")
    assert "if (generationRef.current === reservation) generationRef.current = null;" in body
    assert "bookId, reservation);" in body


def test_fenced_load_does_not_assign_audio_after_stop_during_fetch():
    body = section("  async function loadItem", "  async function refresh")
    fetch = body.index("const payload = await api")
    post_fetch_fence = body.index("!stopFencePassed()", fetch)
    assign_source = body.index("audio.src = cached.src")
    assert fetch < post_fetch_fence < assign_source
