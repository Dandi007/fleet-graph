"""Makefile 的 Docker 测试入口；宿主只负责构建、凭证注入和导出证据。"""

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tarfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime/e2e"


def output(args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare_sources(candidate):
    path = Path(candidate)
    if not path.is_file():
        path = ROOT / "tests/e2e/candidates" / (candidate + ".json")
    manifest = json.loads(path.read_text())
    if manifest.get("schema") != "fleet-e2e.candidate/1":
        raise ValueError("候选 manifest schema 不兼容")
    build = RUNTIME / "build"
    build.mkdir(parents=True, exist_ok=True)
    commits = {}
    for name in ("fleet", "agent-runtime", "katana", "agent-bus", "agent-knowledge"):
        source = manifest[name]
        repo = str(Path(source["repo"]).resolve())
        commit = output(["git", "-C", repo, "rev-parse", source["commit"] + "^{commit}"])
        if commit != source["commit"]:
            raise ValueError("候选输入必须固定完整 commit SHA")
        target = build / name
        stamp = build / (name + ".sha")
        if not target.is_dir() or not stamp.exists() or stamp.read_text() != commit:
            if target.exists():
                shutil.rmtree(target)
            target.mkdir()
            archive = build / (name + ".tar")
            with archive.open("wb") as stream:
                subprocess.run(["git", "-C", repo, "archive", commit], stdout=stream, check=True)
            with tarfile.open(archive) as stream:
                stream.extractall(target, filter="data")
            archive.unlink()
            stamp.write_text(commit)
        commits[name.replace("-", "_") + "_commit"] = commit
    commits["runtime_commit"] = commits.pop("agent_runtime_commit")
    commits["candidate"] = manifest["name"]
    for key in ("config_path", "prompt_dir"):
        if key in manifest:
            commits[key] = manifest[key]
    write_json(build / "source-manifest.json", commits)
    return commits


def prepare_secrets(directory, required):
    directory.mkdir(mode=0o700, parents=True)
    token = os.environ.get("NEW_API_GATEWAY_TOKEN_OPENAI", "")
    secret_path = Path(
        os.environ.get(
            "AGENT_RUNTIME_SECRETS_FILE", str(Path.home() / ".config/agent-shell/secrets.env")
        )
    )
    if not token and required and secret_path.is_file():
        for line in secret_path.read_text().splitlines():
            line = line.strip().removeprefix("export ").strip()
            key, sep, value = line.partition("=")
            if sep and key.strip() == "NEW_API_GATEWAY_TOKEN_OPENAI":
                token = value.strip().strip("\"'")
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if required and not gh_token:
        gh_token = output(["gh", "auth", "token"])
    if required and (not token or not gh_token):
        raise RuntimeError("缺少网关或 GitHub 凭证；未启动测试")
    for name, value in {
        "gateway_token": token,
        "gh_token": gh_token,
        "bus_admin_token": secrets.token_urlsafe(48),
        "bus_gateway_token": secrets.token_urlsafe(48),
    }.items():
        path = directory / name
        path.write_text(value)
        # Compose bind secret 保留宿主权限；父目录 0700，容器内非 root 可读取单文件。
        path.chmod(0o444)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "smoke", "e2e"))
    parser.add_argument("--candidate", default="codex")
    parser.add_argument("--case", choices=("single-repo",), default="single-repo")
    args = parser.parse_args()

    def cancelled(_signum, _frame):
        raise KeyboardInterrupt("测试运行被中止")

    signal.signal(signal.SIGTERM, cancelled)
    os.umask(0o077)
    run_id = "fg-" + uuid.uuid4().hex[:12]
    evidence = RUNTIME / "runs" / run_id
    evidence.mkdir(parents=True)
    env = os.environ.copy()
    env["E2E_RUN_ID"] = run_id
    env["E2E_SECRETS_DIR"] = str(evidence / "secrets")
    env["E2E_HOST_GATEWAY_IP"] = output(
        ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"]
    )
    with socket.socket() as sock:
        sock.bind((env["E2E_HOST_GATEWAY_IP"], 0))
        env["E2E_GATEWAY_RELAY_PORT"] = str(sock.getsockname()[1])
    compose = [
        "docker",
        "compose",
        "--project-name",
        run_id,
        "--file",
        str(ROOT / "tests/e2e/compose.yaml"),
        "--profile",
        "test",
    ]
    transcript = evidence / "commands.log"
    redact_values = []

    def run(arguments, check=True):
        with transcript.open("a") as log:
            log.write("$ " + " ".join(arguments) + "\n")
            log.flush()
            process = subprocess.Popen(
                arguments,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in process.stdout:
                for secret in redact_values:
                    line = line.replace(secret, "[REDACTED]")
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            code = process.wait()
            if check and code:
                raise subprocess.CalledProcessError(code, arguments)
            return code

    status = {
        "schema": "fleet-e2e.execution/1",
        "run_id": run_id,
        "command": args.command,
        "status": "running",
        "e2e_passed": False,
        "native_subscription": "deferred",
    }
    started = False
    try:
        inputs = [
            p
            for p in (ROOT / "tests/e2e").rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        ]
        inputs.extend(ROOT / name for name in ("Makefile", "pyproject.toml", ".dockerignore"))
        status["harness"] = {
            "head": output(["git", "rev-parse", "HEAD"]),
            "dirty": bool(output(["git", "status", "--porcelain"])),
            "sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(inputs)
            },
        }
        prepare_secrets(evidence / "secrets", args.command != "build")
        redact_values.extend(
            p.read_text() for p in (evidence / "secrets").iterdir() if p.read_text()
        )
        config = subprocess.check_output([*compose, "config", "--format", "json"], env=env)
        (evidence / "compose.json").write_bytes(config)
        with (RUNTIME / "build.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            status["sources"] = prepare_sources(args.candidate)
            run([*compose, "build"])
        status["images"] = {
            name: output(
                [
                    "docker",
                    "image",
                    "inspect",
                    service.get("image", f"{run_id}-{name}"),
                    "--format",
                    "{{.Id}}",
                ]
            )
            for name, service in json.loads(config)["services"].items()
        }
        if args.command == "build":
            status["status"] = "build_passed"
            return
        started = True
        run([*compose, "up", "-d", "--wait", "--wait-timeout", "180", "candidate"])
        for service, probe in (
            ("work-folder", "work-folder-read-write"),
            ("agent-bus", "bus-read-write"),
            ("work-folder", "git-sync"),
        ):
            run([*compose, "exec", "-T", service, "python", "/opt/e2e/probe.py", probe])
        run([*compose, "run", "--rm", "runner", "--mode", args.command])
        if args.command == "e2e":
            run(
                [
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "verifier",
                    "--bundle",
                    f"/artifacts/{run_id}",
                    "--repo",
                    "/workspace/fixture",
                    "--output",
                    "/verification/verification.json",
                ]
            )
            status["e2e_passed"] = True
        status["status"] = args.command + "_passed"
    except BaseException as error:
        status["status"] = "failed"
        status["error"] = str(error)
        raise
    finally:
        cleanup_error = None
        try:
            if started:
                stopped = run([*compose, "stop", "--timeout", "30"], check=False) == 0
                run([*compose, "logs", "--no-color"], check=False)
                # 导出失败保留 named volumes，不能删除唯一原始证据。
                exported = stopped & (
                    run(
                        [*compose, "cp", "candidate:/state", str(evidence / "candidate-state")],
                        check=False,
                    )
                    == 0
                )
                run([*compose, "create", "--no-recreate", "runner", "verifier"], check=False)
                exported &= (
                    run(
                        [*compose, "cp", "runner:/artifacts/.", str(evidence / "artifacts")],
                        check=False,
                    )
                    == 0
                )
                for service, source, target in (
                    ("verifier", "/verification", "verification"),
                    ("work-folder", "/data/work-folder", "work-folder"),
                    ("work-folder", "/data/search", "search"),
                    ("agent-bus", "/data/agent-bus", "agent-bus"),
                    ("git-remote", "/data/git", "git-remote"),
                    ("runner", "/workspace", "workspace"),
                ):
                    exported &= (
                        run(
                            [*compose, "cp", f"{service}:{source}", str(evidence / target)],
                            check=False,
                        )
                        == 0
                    )
                redacted = []
                binary_secret_files = []
                for path in evidence.rglob("*"):
                    if not path.is_file() or path.is_symlink() or "secrets" in path.parts:
                        continue
                    data = path.read_bytes()
                    safe = data
                    for secret in redact_values:
                        safe = safe.replace(secret.encode(), b"[REDACTED]")
                    if safe != data:
                        try:
                            data.decode("utf-8")
                            is_text = b"\0" not in data
                        except UnicodeDecodeError:
                            is_text = False
                        if is_text:
                            path.write_bytes(safe)
                            redacted.append(str(path.relative_to(evidence)))
                        else:
                            # 二进制不能字节替换；保留原卷，私有导出不作为可分享证据。
                            binary_secret_files.append(str(path.relative_to(evidence)))
                status["redacted_files"] = redacted
                status["private_binary_secret_files"] = binary_secret_files
                if binary_secret_files:
                    exported = False
                status["evidence_exported"] = exported
                if exported:
                    code = run([*compose, "down", "--volumes", "--remove-orphans"], check=False)
                    if code:
                        raise RuntimeError("Docker 清理失败，需按 project name 清理残留")
                    status["cleanup"] = "removed"
                else:
                    run([*compose, "stop"], check=False)
                    status["cleanup"] = "stopped_volumes_preserved"
                    raise RuntimeError("证据导出失败；已停止容器并保留 named volumes")
        except BaseException as error:
            cleanup_error = error
            status["cleanup_error"] = str(error)
            status["status"] = "failed"
            status["e2e_passed"] = False
        finally:
            shutil.rmtree(evidence / "secrets", ignore_errors=True)
            write_json(evidence / "execution.json", status)
            print(f"测试证据：{evidence}\n状态：{status['status']}", flush=True)
        if cleanup_error:
            raise cleanup_error


if __name__ == "__main__":
    main()
