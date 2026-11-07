"""The setup/deploy requirement preflight.

The launchers run ``python -m aether.setup.requirements`` before the wizard and
before the deploy engine, so these tests pin what it reports and what it tries
to install. All probes are injected, so nothing here touches the network.
"""

from __future__ import annotations

import subprocess

from aether.setup import requirements


def _proc(code: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["x"], returncode=code)


def test_python_version_floor() -> None:
    assert requirements.python_ok((3, 12)) is True
    assert requirements.python_ok((3, 14)) is True
    assert requirements.python_ok((3, 11)) is False


def test_missing_packages_uses_the_finder() -> None:
    present = {"rich": object()}
    assert requirements.missing_packages(
        {"rich": "rich", "prompt_toolkit": "prompt_toolkit"},
        find_spec=lambda module: present.get(module),
    ) == ["prompt_toolkit"]


def test_missing_commands_reports_absent_tools() -> None:
    assert requirements.missing_commands(
        ("curl", "base64"), which=lambda name: "/usr/bin/curl" if name == "curl" else None
    ) == ["base64"]


def test_check_setup_only_looks_at_packages() -> None:
    result = requirements.check(
        "setup",
        version_info=(3, 12),
        find_spec=lambda module: None,
        which=lambda name: None,
    )
    assert result.packages == list(requirements.SETUP_PACKAGES)
    assert result.commands == []
    assert result.any is True


def test_check_deploy_only_looks_at_commands() -> None:
    result = requirements.check(
        "deploy",
        version_info=(3, 12),
        find_spec=lambda module: None,
        which=lambda name: None,
    )
    assert result.commands == ["curl", "base64"]
    assert result.packages == []


def test_windows_deploy_does_not_require_base64() -> None:
    assert requirements.deploy_commands("win32") == ("curl",)
    assert requirements.deploy_commands("linux") == ("curl", "base64")
    result = requirements.check(
        "deploy",
        version_info=(3, 12),
        find_spec=lambda module: None,
        which=lambda name: "/usr/bin/curl" if name == "curl" else None,
        platform="win32",
    )
    assert result.commands == []
    assert result.any is False


def test_deploy_does_not_block_on_an_old_interpreter() -> None:
    result = requirements.check(
        "deploy",
        version_info=(3, 8),
        which=lambda name: "/usr/bin/" + name,
    )
    assert result.any is False


def test_install_python_packages_falls_back_to_user() -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        seen.append(command)
        return _proc(1 if "--user" not in command else 0)

    assert requirements.install_python_packages(["rich"], python="py", run=run) is True
    assert seen[0] == ["py", "-m", "pip", "install", "--quiet", "rich"]
    assert seen[1] == ["py", "-m", "pip", "install", "--quiet", "--user", "rich"]


def test_install_python_packages_reports_failure() -> None:
    assert (
        requirements.install_python_packages(
            ["rich"], python="py", run=lambda command, **kwargs: _proc(1)
        )
        is False
    )


def test_install_system_command_picks_the_first_manager() -> None:
    seen: list[list[str]] = []

    def which(name: str) -> str | None:
        return "/usr/bin/apt-get" if name == "apt-get" else None

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        seen.append(command)
        return _proc(0)

    assert requirements.install_system_command("curl", which=which, run=run) is True
    assert seen == [["sudo", "-n", "apt-get", "install", "-y", "curl"]]


def test_install_system_command_maps_base64_to_coreutils() -> None:
    seen: list[list[str]] = []

    def which(name: str) -> str | None:
        return "/usr/bin/apt-get" if name == "apt-get" else None

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        seen.append(command)
        return _proc(0)

    assert requirements.install_system_command("base64", which=which, run=run) is True
    assert seen == [["sudo", "-n", "apt-get", "install", "-y", "coreutils"]]


def test_install_system_command_without_a_manager_fails() -> None:
    assert (
        requirements.install_system_command(
            "curl", which=lambda name: None, run=lambda *a, **k: _proc(0)
        )
        is False
    )


def test_ensure_installs_then_rechecks() -> None:
    calls = {"installs": 0, "lookups": 0}

    def find_spec(module: str):
        calls["lookups"] += 1
        # missing on the first check, present after the install
        return object() if calls["installs"] else None

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls["installs"] += 1
        return _proc(0)

    missing = requirements.ensure(
        "setup",
        install=True,
        version_info=(3, 12),
        find_spec=find_spec,
        which=lambda name: None,
        run=run,
        out=lambda line: None,
    )
    assert calls["installs"] == 1
    assert missing.any is False


def test_ensure_no_install_never_runs_anything() -> None:
    ran: list[list[str]] = []
    missing = requirements.ensure(
        "setup",
        install=False,
        version_info=(3, 12),
        find_spec=lambda module: None,
        which=lambda name: None,
        run=lambda command, **kwargs: (ran.append(command), _proc(0))[1],
        out=lambda line: None,
    )
    assert ran == []
    assert missing.packages == list(requirements.SETUP_PACKAGES)


def test_main_reports_missing_requirements(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        requirements,
        "ensure",
        lambda *a, **k: requirements.Missing(python_too_old=True, packages=["rich"]),
    )
    assert requirements.main(["--mode", "setup"]) == 1
    out = capsys.readouterr().out
    assert "Python 3.12+" in out
    assert "missing Python packages: rich" in out


def test_main_passes_when_satisfied(monkeypatch, capsys) -> None:
    monkeypatch.setattr(requirements, "ensure", lambda *a, **k: requirements.Missing())
    assert requirements.main(["--mode", "deploy"]) == 0
    assert "requirements satisfied" in capsys.readouterr().out


def test_venv_python_per_platform() -> None:
    assert requirements.venv_python("/tmp/v", platform="linux").as_posix().endswith("bin/python")
    assert (
        requirements.venv_python("C:/v", platform="win32").as_posix().endswith("Scripts/python.exe")
    )


def test_bootstrap_venv_creates_then_installs(tmp_path) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(command)
        return _proc(0)

    interpreter = requirements.bootstrap_venv(
        tmp_path / "venv",
        ["rich"],
        python="py",
        platform="linux",
        run=run,
        out=lambda line: None,
    )
    assert interpreter is not None and interpreter.as_posix().endswith("bin/python")
    assert calls[0] == ["py", "-m", "venv", str(tmp_path / "venv")]
    assert calls[1] == [str(interpreter), "-m", "pip", "install", "--quiet", "rich"]


def test_bootstrap_venv_reuses_an_existing_environment(tmp_path) -> None:
    interpreter = requirements.venv_python(tmp_path / "venv", platform="linux")
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(command)
        return _proc(0)

    result = requirements.bootstrap_venv(
        tmp_path / "venv",
        ["rich"],
        python="py",
        platform="linux",
        run=run,
        out=lambda line: None,
    )
    assert result == interpreter
    assert all("venv" not in call for call in calls if call[:1] == ["py"])  # no recreate


def test_bootstrap_venv_fails_when_creation_fails(tmp_path) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return _proc(1 if "venv" in command[1:3] else 0)

    assert (
        requirements.bootstrap_venv(
            tmp_path / "v", ["rich"], python="py", platform="linux", run=run, out=lambda line: None
        )
        is None
    )


def test_main_bootstrap_reports_ready(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(requirements, "bootstrap_venv", lambda *a, **k: tmp_path / "v/bin/python")
    monkeypatch.setattr(requirements, "requirements_check", lambda interpreter: [])
    assert requirements.main(["--bootstrap", str(tmp_path / "v")]) == 0
    assert "virtual environment ready" in capsys.readouterr().out


def test_main_bootstrap_reports_still_missing(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(requirements, "bootstrap_venv", lambda *a, **k: tmp_path / "v/bin/python")
    monkeypatch.setattr(requirements, "requirements_check", lambda interpreter: ["rich"])
    assert requirements.main(["--bootstrap", str(tmp_path / "v")]) == 1
    assert "still cannot import: rich" in capsys.readouterr().out
