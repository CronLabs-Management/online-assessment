"""
Code execution behind a narrow interface.

Candidate code never runs on the web server. `Judge0Runner` sends each test case to
an isolated sandbox service (Judge0 CE or a compatible API). When no service is
configured, `UnconfiguredRunner` raises RunnerError so submissions are marked as
runner errors instead of being silently graded wrong.
"""

import base64
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

try:
    import resource
except ImportError:
    resource = None

from django.conf import settings


class RunnerError(Exception):
    """The sandbox could not run the code at all (not a wrong answer)."""


@dataclass
class CaseOutcome:
    status: str  # one of CodingTestResult.Status values
    stdout: str = ""
    stderr: str = ""
    compile_output: str = ""
    time_ms: int = 0
    memory_kb: int = 0


# Judge0 CE language IDs for the languages we offer.
JUDGE0_LANGUAGE_IDS = {
    "python": 71,      # Python (3.8.1)
    "javascript": 63,  # JavaScript (Node.js 12.14.0)
    "java": 62,        # Java (OpenJDK 13.0.1)
    "c": 50,           # C (GCC 9.2.0)
    "cpp": 54,         # C++ (GCC 9.2.0)
    "c++": 54,         # C++ alias
}


def normalize_output(text):
    """Compare outputs ignoring trailing whitespace on each line and at the end."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


class BaseRunner:
    def run_case(self, language, source_code, stdin):
        raise NotImplementedError


class UnconfiguredRunner(BaseRunner):
    def run_case(self, language, source_code, stdin):
        raise RunnerError("The code runner is not configured yet.")


class Judge0Runner(BaseRunner):
    """Judge0 CE REST API, using wait=true so each case returns synchronously."""

    def __init__(self, base_url, api_key="", auth_header="X-Auth-Token", timeout=20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.auth_header = auth_header
        self.timeout = timeout

    def _post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/submissions?base64_encoded=true&wait=true",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.api_key:
            request.add_header(self.auth_header, self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RunnerError(f"Sandbox request failed: {exc}") from exc

    @staticmethod
    def _decode(value):
        if not value:
            return ""
        try:
            return base64.b64decode(value).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""

    def run_case(self, language, source_code, stdin):
        language_id = JUDGE0_LANGUAGE_IDS.get(language)
        if language_id is None:
            raise RunnerError(f"Unsupported language: {language}")

        payload = {
            "language_id": language_id,
            "source_code": base64.b64encode(source_code.encode("utf-8")).decode("ascii"),
            "stdin": base64.b64encode((stdin or "").encode("utf-8")).decode("ascii"),
            "cpu_time_limit": settings.CODE_CPU_TIME_LIMIT_S,
            "memory_limit": settings.CODE_MEMORY_LIMIT_KB,
            "max_file_size": settings.CODE_OUTPUT_LIMIT_KB,
            "enable_network": False,
        }
        result = self._post(payload)
        status_id = (result.get("status") or {}).get("id")
        stdout = self._decode(result.get("stdout"))
        stderr = self._decode(result.get("stderr"))
        compile_output = self._decode(result.get("compile_output"))
        time_ms = int(float(result.get("time") or 0) * 1000)
        memory_kb = int(result.get("memory") or 0)

        # Judge0 status IDs: 1 In Queue and 2 Processing are not finished, so they are runner
        # failures, never wrong answers. 3 Accepted and 4 Wrong Answer both hand the output to the
        # grader. 5 Time Limit Exceeded, 6 Compilation Error, 7-12 and 14 runtime errors.
        # 0 (missing), 13 (internal error) and anything unknown are runner failures.
        if status_id in (None, 0, 1, 2, 13):
            raise RunnerError("The sandbox did not return a finished result.")
        if status_id == 6:
            return CaseOutcome("compile_error", stdout, stderr, compile_output, time_ms, memory_kb)
        if status_id == 5:
            return CaseOutcome("timeout", stdout, stderr, compile_output, time_ms, memory_kb)
        if status_id in (3, 4):
            return CaseOutcome("ok", stdout, stderr, compile_output, time_ms, memory_kb)
        if status_id == 14 or 7 <= status_id <= 12:
            return CaseOutcome("runtime_error", stdout, stderr, compile_output, time_ms, memory_kb)
        raise RunnerError(f"Unexpected sandbox status {status_id}.")


class LocalRunner(BaseRunner):
    """
    Development-only executor: runs each case as a local process with CPU, memory and
    output limits inside a throwaway directory.

    This is NOT a security boundary. Submitted code runs with the same OS user as the
    web server and can read files or open network connections. Use it only with
    CODE_RUNNER=local on a machine where you control every submission. Production
    must use the isolated Judge0 service (or an equivalent sandbox on a separate host).
    """

    COMMANDS = {
        "python": (["python3", "-I", "-B"], "main.py"),
        "javascript": (["node"], "main.js"),
        "c": (["gcc", "-O2", "main.c", "-o", "main.out"], "main.c"),
        "cpp": (["g++", "-O2", "main.cpp", "-o", "main.out"], "main.cpp"),
        "java": (["javac", "Main.java"], "Main.java"),
    }
    # Address space multiplier per runtime/compiler.
    ADDRESS_SPACE_MULTIPLIER = {
        "python": 1,
        "javascript": 8,
        "java": 12,
        "c": 2,
        "cpp": 2,
    }

    def run_case(self, language, source_code, stdin):
        language = "cpp" if language == "c++" else language
        spec = self.COMMANDS.get(language)
        if spec is None:
            raise RunnerError(f"Unsupported language: {language}")
        argv, filename = list(spec[0]), spec[1]
        if language == "python":
            if os.name == "nt":
                py_bin = sys.executable
            else:
                py_bin = shutil.which("python3") or shutil.which("python") or sys.executable
            argv[0] = py_bin
        if shutil.which(argv[0]) is None:
            raise RunnerError(f"{argv[0]} is not installed on this machine.")

        cpu_limit = max(1, math.ceil(settings.CODE_CPU_TIME_LIMIT_S))
        address_bytes = settings.CODE_MEMORY_LIMIT_KB * 1024 * self.ADDRESS_SPACE_MULTIPLIER.get(language, 1)
        output_bytes = settings.CODE_OUTPUT_LIMIT_KB * 1024

        def apply_limits():
            if resource is not None:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 1))
                resource.setrlimit(resource.RLIMIT_AS, (address_bytes, address_bytes))
                # Output goes to files, and RLIMIT_FSIZE stops a file growing past the limit.
                resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes, output_bytes))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if hasattr(os, "setsid"):
                os.setsid()

        env = {
            "PATH": os.environ.get("PATH", "") if os.name == "nt" else "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with tempfile.TemporaryDirectory(prefix="case-") as workdir:
            with open(os.path.join(workdir, filename), "w", encoding="utf-8") as handle:
                handle.write(source_code)

            exec_argv = argv + [filename]
            if language in ("c", "cpp"):
                compile_proc = subprocess.run(
                    argv, check=False, cwd=workdir, env=env, capture_output=True, text=True, timeout=10
                )
                if compile_proc.returncode != 0:
                    return CaseOutcome("compile_error", compile_output=compile_proc.stderr or compile_proc.stdout)
                exec_argv = ["./main.out"]
            elif language == "java":
                compile_proc = subprocess.run(
                    argv, check=False, cwd=workdir, env=env, capture_output=True, text=True, timeout=10
                )
                if compile_proc.returncode != 0:
                    return CaseOutcome("compile_error", compile_output=compile_proc.stderr or compile_proc.stdout)
                exec_argv = ["java", "-Xmx256m", "Main"]

            out_path = os.path.join(workdir, "stdout.bin")
            err_path = os.path.join(workdir, "stderr.bin")
            started = time.monotonic()
            with open(out_path, "wb") as out_file, open(err_path, "wb") as err_file:
                try:
                    proc = subprocess.run(
                        exec_argv,
                        check=False,  # exit status is inspected below
                        input=(stdin or "").encode("utf-8"),
                        cwd=workdir,
                        env=env,
                        stdout=out_file,
                        stderr=err_file,
                        timeout=cpu_limit + 2,
                        preexec_fn=apply_limits if hasattr(os, "setsid") else None,
                    )
                except subprocess.TimeoutExpired:
                    return CaseOutcome("timeout", time_ms=int((time.monotonic() - started) * 1000))
                except OSError as exc:
                    raise RunnerError(f"Could not start {exec_argv[0]}: {exc}") from exc
            elapsed_ms = int((time.monotonic() - started) * 1000)
            with open(out_path, "rb") as handle:
                stdout = handle.read(output_bytes).decode("utf-8", errors="replace")
            with open(err_path, "rb") as handle:
                stderr = handle.read(output_bytes).decode("utf-8", errors="replace")

        sigxcpu = getattr(signal, "SIGXCPU", None)
        if sigxcpu is not None and proc.returncode == -sigxcpu:
            return CaseOutcome("timeout", stdout, stderr, time_ms=elapsed_ms)
        sigxfsz = getattr(signal, "SIGXFSZ", None)
        if sigxfsz is not None and proc.returncode == -sigxfsz:
            return CaseOutcome("runtime_error", stdout, stderr + "\nOutput limit exceeded.", time_ms=elapsed_ms)
        if proc.returncode != 0:
            return CaseOutcome("runtime_error", stdout, stderr, time_ms=elapsed_ms)
        return CaseOutcome("ok", stdout, stderr, time_ms=elapsed_ms)


def get_runner():
    mode = settings.CODE_RUNNER
    if mode == "judge0" or (mode == "" and settings.JUDGE0_URL):
        if not settings.JUDGE0_URL:
            return UnconfiguredRunner()
        return Judge0Runner(
            settings.JUDGE0_URL,
            api_key=settings.JUDGE0_API_KEY,
            auth_header=settings.JUDGE0_AUTH_HEADER,
        )
    if mode == "local":
        return LocalRunner()
    return UnconfiguredRunner()
