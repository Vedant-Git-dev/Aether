"""The interactive setup wizard.

Two layers: the pure helpers (glyphs, catalogs, answers I/O, artifacts) and
the real prompt_toolkit applications driven headlessly with a pipe. The E2E
tests send only arrow keys, Space and Enter — never a typed y/n — and check
that Ctrl+C and Backspace leave nothing half-written.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.plain_text import PlainTextOutput
from rich.console import Console

from aether.setup import artifacts, brand, catalogs
from aether.setup.capabilities import Glyphs, color_enabled, glyphs, stdout_is_tty
from aether.setup.cli import main
from aether.setup.plan import SetupPlan, load_answers, write_answers, write_secrets
from aether.setup.prompts import (
    UI,
    Cancelled,
    ChoiceList,
    GoBack,
    ask_secret,
    ask_text,
    hint_line,
    select_many,
    select_one,
)
from aether.setup.stages import SetupContext, Stage, build_flow
from aether.setup.wizard import collect, persist, preload_state, run_flow

UNICODE = Glyphs(selected="●", unselected="○", pointer="❯", unicode=True)
ASCII = Glyphs(selected="(*)", unselected="( )", pointer=">", unicode=False)


def make_ui(pipe=None, *, color: bool = False, marks: Glyphs = UNICODE, output=None) -> UI:
    return UI(
        console=Console(record=True, no_color=True),
        glyphs=marks,
        color=color,
        input=pipe,
        output=output if output is not None else DummyOutput(),
    )


def record_ui() -> UI:
    return UI(console=Console(record=True), glyphs=UNICODE, color=False)


def ctx_for(plan: SetupPlan | None = None, secrets: dict[str, str] | None = None) -> SetupContext:
    return SetupContext(
        plan=plan or SetupPlan(),
        secrets=dict(secrets or {}),
        ui=record_ui(),
        root=catalogs.repo_root(),
    )


# -- capabilities ----------------------------------------------------------------


def test_stringio_is_not_a_tty() -> None:
    assert stdout_is_tty(io.StringIO()) is False


def test_no_color_disables_color_even_on_a_tty() -> None:
    class FakeTTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert color_enabled(FakeTTY(), env={"NO_COLOR": "1"}) is False
    assert color_enabled(FakeTTY(), env={}) is True


def test_glyphs_fall_back_to_ascii_when_requested(monkeypatch) -> None:
    class FakeTTY(io.StringIO):
        encoding = "utf-8"

        def isatty(self) -> bool:
            return True

    assert glyphs(FakeTTY(), env={}).unicode is True
    assert glyphs(FakeTTY(), env={"AETHER_ASCII": "1"}).unicode is False
    assert glyphs(io.StringIO(), env={}).unicode is False


# -- brand -----------------------------------------------------------------------


def test_logo_for_unicode_and_ascii() -> None:
    assert brand.logo_for(True) == brand.UNICODE_LOGO
    assert brand.logo_for(False) == brand.ASCII_LOGO


def test_gradient_logo_reveals_the_requested_rows() -> None:
    one = brand.gradient_logo(brand.UNICODE_LOGO, reveal=1)
    full = brand.gradient_logo(brand.UNICODE_LOGO)
    assert one.plain.count("\n") == 1
    assert full.plain.count("\n") == len(brand.UNICODE_LOGO)


def test_intro_frames_end_on_the_tagline() -> None:
    frames = list(brand.intro_frames(brand.UNICODE_LOGO))
    assert len(frames) == len(brand.UNICODE_LOGO) + 2
    buffer = io.StringIO()
    Console(file=buffer, no_color=True).print(frames[-1])
    assert brand.TAGLINE in buffer.getvalue()


def test_static_intro_does_not_sleep() -> None:
    buffer = io.StringIO()
    slept: list[float] = []
    brand.render_intro(
        Console(file=buffer, no_color=True),
        animate=False,
        unicode=True,
        sleep=slept.append,
    )
    assert slept == []
    assert "AETHER" in buffer.getvalue() or "▄▀█" in buffer.getvalue()


def test_animated_intro_sleeps_and_stays_under_budget() -> None:
    slept: list[float] = []
    brand.render_intro(
        Console(file=io.StringIO(), no_color=True),
        animate=True,
        unicode=True,
        sleep=slept.append,
    )
    assert len(slept) == len(brand.UNICODE_LOGO) + 2
    assert 0 < sum(slept) < 2.5


# -- prompts ---------------------------------------------------------------------


def test_single_choice_draws_circles_and_a_cursor() -> None:
    ui = make_ui(color=True)
    prompt = ChoiceList(ui, "Database", ["neon", "existing"], multi=False)
    text = "".join(fragment for _style, fragment in prompt._fragments())
    assert "●" in text and "○" in text  # the cursor row is selected
    styles = [style for style, _ in prompt._fragments()]
    assert "class:cursor" in styles
    assert "class:cursor class:circle-on" in styles
    assert "class:pointer" in styles


def test_choice_prompt_has_readable_spacing() -> None:
    prompt = ChoiceList(make_ui(), "Database", ["neon", "existing"], multi=False)
    text = "".join(fragment for _style, fragment in prompt._fragments())
    assert text.startswith("\n")  # a breath before the question
    assert "Database\n\n" in text  # and after it
    assert text.endswith("\n\n")  # room before the next output


def test_cursor_style_is_not_reverse_video() -> None:
    from aether.setup.theme import prompt_style

    rules = dict(getattr(prompt_style(True), "style_rules", []))
    assert "reverse" not in rules["cursor"]
    assert "underline" in rules["cursor"]


def test_multi_choice_can_show_several_selected() -> None:
    ui = make_ui()
    prompt = ChoiceList(ui, "Services", ["a", "b", "c"], multi=True, default_selected={0, 2})
    text = "".join(fragment for _style, fragment in prompt._fragments())
    assert text.count("●") == 2
    assert text.count("○") == 1
    assert "2 selected" in text


def test_ascii_fallback_marks() -> None:
    ui = make_ui(marks=ASCII)
    prompt = ChoiceList(ui, "q", ["a", "b"], multi=False)
    text = "".join(fragment for _style, fragment in prompt._fragments())
    assert "(*)" in text and "( )" in text


def test_hint_line_reads_plainly() -> None:
    assert hint_line(multi=False, unicode=True).startswith("↑↓ move")
    assert "space toggle" in hint_line(multi=True, unicode=True)
    assert "up/down" in hint_line(multi=True, unicode=False)


def test_select_one_moves_with_arrows_and_confirms_with_enter() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\x1b[B\r")  # down, down, enter
        assert select_one(make_ui(pipe), "q", ["a", "b", "c"]) == 2


def test_select_one_space_confirms_the_highlighted_option() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B ")
        assert select_one(make_ui(pipe), "q", ["a", "b", "c"]) == 1


def test_select_many_toggles_with_space() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text(" \x1b[B \r")  # toggle a, down, toggle b, enter
        assert select_many(make_ui(pipe), "q", ["a", "b", "c"]) == [0, 1]


def test_backspace_goes_back() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x08")
        with pytest.raises(GoBack):
            select_one(make_ui(pipe), "q", ["a", "b"])


def test_ctrl_c_is_a_clean_keyboard_interrupt() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            select_one(make_ui(pipe), "q", ["a", "b"])


def test_text_takes_the_default_on_enter() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\r")
        assert ask_text(make_ui(pipe), "name", default="aether") == "aether"


def test_text_ctrl_c_is_a_clean_interrupt() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            ask_text(make_ui(pipe), "name")


def test_secret_is_masked_in_the_rendered_prompt() -> None:
    stdout = io.StringIO()
    with create_pipe_input() as pipe:
        pipe.send_text("s3cr3t-value\r")
        value = ask_secret(make_ui(pipe, output=PlainTextOutput(stdout=stdout)), "API key")
    assert value == "s3cr3t-value"
    assert "s3cr3t-value" not in stdout.getvalue()  # the prompt never echoes it


# -- answers I/O -----------------------------------------------------------------


def test_toml_round_trip(tmp_path: Path) -> None:
    plan = SetupPlan(
        database_mode="neon",
        llm_provider="openai",
        llm_model="openai/gpt-5.1",
        services=["Web search"],
        apps=["gmail"],
        channels=["telegram"],
        plan="starter",
        local_copy=True,
    )
    path = write_answers(tmp_path / "answers.toml", plan)
    loaded, secrets = load_answers(path)
    assert loaded == plan
    assert secrets == {}


def test_legacy_env_answers_still_load(tmp_path: Path) -> None:
    path = tmp_path / "answers.env"
    path.write_text(
        "DATABASE_MODE=existing\n"
        "LLM_PROVIDER=gemini\n"
        "SERVICES=Web search,Voice-note transcription\n"
        "APPS=gmail,calendar\n"
        "LOCAL_COPY=yes\n"
        "DATABASE_URL=postgresql://user:pass@host/db?sslmode=require\n"
    )
    plan, secrets = load_answers(path)
    assert plan.database_mode == "existing"
    assert plan.llm_provider == "gemini"
    assert plan.services == ["Web search", "Voice-note transcription"]
    assert plan.apps == ["gmail", "calendar"]
    assert plan.local_copy is True
    assert secrets["DATABASE_URL"].startswith("postgresql://")


def test_secrets_file_is_owner_only(tmp_path: Path) -> None:
    path = write_secrets(tmp_path / "secrets.env", {"AETHER_TOKEN": "abc"})
    assert path.read_text() == "AETHER_TOKEN=abc\n"
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600


# -- catalogs --------------------------------------------------------------------


def test_providers_come_from_the_config() -> None:
    assert "anthropic" in catalogs.providers()


def test_services_make_choices_but_never_screen_capture_or_semantic_memory() -> None:
    services = [choice.label for choice in catalogs.optional_services(catalogs.repo_root())]
    assert any(label == "Web search" for label in services)
    assert not any("Screen capture" in label for label in services)
    assert not any("Semantic memory" in label for label in services)


def test_direct_apps_and_channels() -> None:
    app_keys = {choice.key for choice in catalogs.direct_apps()}
    assert {"gmail", "calendar", "github"}.issubset(app_keys)
    channel_keys = {choice.key for choice in catalogs.chat_channels()}
    assert {"telegram", "discord"}.issubset(channel_keys)


def test_apply_dependencies_clears_unselected_services() -> None:
    plan = SetupPlan(services=[], image_provider="gemini", workbench_provider="modal")
    catalogs.apply_dependencies(plan)
    assert plan.image_provider == "none"
    assert plan.workbench_provider == "none"


# -- artifacts -------------------------------------------------------------------


def test_generated_config_reflects_the_plan() -> None:
    plan = SetupPlan(llm_provider="openai", llm_model="openai/gpt-5.1", channels=["telegram"])
    config = artifacts.build_config(plan)
    assert "provider: openai" in config
    assert "model: openai/gpt-5.1" in config
    assert "enabled: true" in config


def test_build_env_maps_secret_names() -> None:
    plan = SetupPlan(llm_provider="anthropic")
    env = artifacts.build_env(plan, {"ANTHROPIC_KEY": "sk-x", "AETHER_TOKEN": "t", "": ""})
    assert env["ANTHROPIC_API_KEY"] == "sk-x"
    assert env["AETHER_TOKEN"] == "t"
    assert "AETHER_CONFIG_B64" in env
    assert "PYTHON_VERSION" in env


def test_core_secrets_are_generated_once() -> None:
    secrets: dict[str, str] = {}
    artifacts.ensure_core_secrets(secrets)
    first = dict(secrets)
    artifacts.ensure_core_secrets(secrets)
    assert secrets == first
    assert set(first) == {
        "AETHER_ENCRYPTION_KEY",
        "AETHER_TOKEN",
        "TOKEN_ENC_KEY",
        "TOKEN_ENC_KEY_ID",
    }


def test_render_yaml_prompts_for_secrets_and_adds_a_disk_on_paid() -> None:
    free = artifacts.render_yaml(SetupPlan(plan="free"))
    paid = artifacts.render_yaml(SetupPlan(plan="starter", service_name="my-aether"))
    assert "sync: false" in free
    assert "disk:" not in free
    assert "disk:" in paid
    assert "name: my-aether" in paid


# -- the flow --------------------------------------------------------------------


def test_flow_covers_every_question_in_order() -> None:
    keys = [stage.key for stage in build_flow(ctx_for())]
    assert keys[0] == "database"
    assert keys[-1] == "uptime_api_key"
    for expected in ("provider", "model", "services", "apps", "channels", "plan", "region"):
        assert expected in keys


def test_dependent_questions_appear_only_when_relevant() -> None:
    ctx = ctx_for()
    stages = {stage.key: stage for stage in build_flow(ctx)}
    assert stages["image_provider"].visible(ctx) is False
    assert stages["workbench_provider"].visible(ctx) is False
    assert stages["repo_url"].visible(ctx) is False
    ctx.plan.deploy_source = "fork"
    assert stages["repo_url"].visible(ctx) is True
    ctx.plan.services = ["Image generation", "Background jobs"]
    assert stages["image_provider"].visible(ctx) is True
    assert stages["workbench_provider"].visible(ctx) is True
    ctx.plan.uptime_mode = "email"
    assert stages["alert_email"].visible(ctx) is True
    ctx.plan.apps = ["gmail"]
    assert stages["google_client_id"].visible(ctx) is True


def test_non_interactive_mode_fills_defaults() -> None:
    ctx = ctx_for()
    run_flow(ctx, interactive=False, stages=build_flow(ctx))
    assert ctx.plan.database_mode == "neon"
    assert ctx.plan.llm_provider == "anthropic"
    assert ctx.plan.llm_model
    assert ctx.plan.plan == "free"


def test_back_navigation_revisits_the_previous_stage() -> None:
    calls: list[str] = []
    first = Stage("a", lambda ctx: calls.append("a"), lambda ctx: None)
    second_calls = 0

    def second(ctx: SetupContext) -> None:
        nonlocal second_calls
        second_calls += 1
        calls.append("b")
        if second_calls == 1:
            raise GoBack

    second_stage = Stage("b", second, lambda ctx: None)
    run_flow(ctx_for(), interactive=True, stages=[first, second_stage])
    assert calls == ["a", "b", "a", "b"]


def test_back_at_the_first_question_cancels() -> None:
    first = Stage("a", lambda ctx: (_ for _ in ()).throw(GoBack()), lambda ctx: None)
    with pytest.raises(Cancelled):
        run_flow(ctx_for(), interactive=True, stages=[first])


def test_ctrl_c_becomes_a_clean_cancellation() -> None:
    first = Stage(
        "a",
        lambda ctx: (_ for _ in ()).throw(KeyboardInterrupt()),
        lambda ctx: None,
    )
    with pytest.raises(Cancelled):
        run_flow(ctx_for(), interactive=True, stages=[first])


# -- persistence -----------------------------------------------------------------


def test_persist_writes_every_artifact(tmp_path: Path) -> None:
    ctx = ctx_for()
    ctx.secrets["DATABASE_URL"] = "postgresql://x"
    result = persist(ctx, tmp_path)
    for path in (
        result.answers_path,
        result.env_answers_path,
        result.secrets_path,
        result.config_path,
        result.render_path,
    ):
        assert path is not None and path.is_file()
    assert 'database_mode = "neon"' in result.answers_path.read_text()
    assert "DATABASE_MODE=neon" in result.env_answers_path.read_text()


def test_preload_state_reads_an_earlier_run(tmp_path: Path) -> None:
    persist(ctx_for(), tmp_path)
    plan, secrets = preload_state(tmp_path)
    assert plan is not None and plan.database_mode == "neon"
    assert "AETHER_TOKEN" in secrets


# -- CLI -------------------------------------------------------------------------


def test_cli_without_a_tty_explains_the_flags() -> None:
    stream = io.StringIO()
    assert main(["--no-animation"], stream=stream) == 2
    assert "--answers answers.toml" in stream.getvalue()
    assert "--yes" in stream.getvalue()


def test_cli_yes_writes_state_without_a_tty(tmp_path: Path) -> None:
    assert (
        main(["--yes", "--no-animation", "--state-dir", str(tmp_path)], stream=io.StringIO()) == 0
    )
    assert (tmp_path / "answers.toml").is_file()
    assert (tmp_path / "secrets.env").is_file()
    assert (tmp_path / "config.generated.yaml").is_file()


def test_cli_answers_toml_drives_a_scripted_install(tmp_path: Path) -> None:
    source = tmp_path / "answers.toml"
    write_answers(source, SetupPlan(llm_provider="openai", llm_model="openai/gpt-5.1"))
    state = tmp_path / "state"
    assert (
        main(
            ["--answers", str(source), "--no-animation", "--state-dir", str(state)],
            stream=io.StringIO(),
        )
        == 0
    )
    plan, _ = load_answers(state / "answers.toml")
    assert plan.llm_provider == "openai"
    assert plan.llm_model == "openai/gpt-5.1"


def test_cli_dry_run_writes_nothing(tmp_path: Path) -> None:
    assert main(["--yes", "--dry-run", "--state-dir", str(tmp_path)], stream=io.StringIO()) == 0
    assert list(tmp_path.iterdir()) == []


# -- end to end: only arrows, Space and Enter ------------------------------------


def test_e2e_enter_through_the_whole_flow() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\r" * 40)
        ctx = collect(ui=make_ui(pipe), root=catalogs.repo_root(), interactive=True)
    assert ctx.plan.database_mode == "neon"
    assert ctx.plan.llm_provider == "anthropic"
    assert ctx.plan.plan == "free"
    assert ctx.plan.deploy_source == "public"
    assert ctx.plan.local_copy is False


def test_e2e_arrow_keys_pick_a_different_option() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\r" + "\r" * 40)  # down, enter, then accept the rest
        ctx = collect(ui=make_ui(pipe), root=catalogs.repo_root(), interactive=True)
    assert ctx.plan.database_mode == "existing"


def test_e2e_space_toggles_a_multi_select() -> None:
    with create_pipe_input() as pipe:
        # db, provider, model, then toggle the first service, then accept the rest
        pipe.send_text("\r\r\r \r" + "\r" * 40)
        ctx = collect(ui=make_ui(pipe), root=catalogs.repo_root(), interactive=True)
    assert "Web search" in ctx.plan.services


def test_e2e_ctrl_c_cancels_cleanly() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x03")
        with pytest.raises(Cancelled):
            collect(ui=make_ui(pipe), root=catalogs.repo_root(), interactive=True)


def test_e2e_backspace_returns_to_the_previous_question() -> None:
    # Answer the database question with the second option, then go back and
    # take the first: the second stage must run twice.
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\r" + "\x08" + "\x1b[A\r" + "\r" * 40)
        ctx = collect(ui=make_ui(pipe), root=catalogs.repo_root(), interactive=True)
    assert ctx.plan.database_mode == "neon"
