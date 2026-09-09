"""Report resolved versions and memory without changing the machine."""
import json
import subprocess
from toolchain import ROOT, LOCK, environment, executable, memory_info


def main():
    result = {"schema_version": 1, "digital_revision": LOCK["digital_revision"], "memory": memory_info(), "tools": {}}
    for name, flags in {"java": ["-version"], "javac": ["-version"], "python": ["--version"],
                        "yosys": ["-V"], "iverilog": ["-V"], "vvp": ["-V"],
                        "cmake": ["--version"], "g++": ["--version"], "ninja": ["--version"]}.items():
        try:
            path = executable(name)
            p = subprocess.run([path, *flags], env=environment(path), capture_output=True, text=True, errors="replace", timeout=20)
            lines = [line for line in (p.stdout + p.stderr).splitlines()
                     if line.strip() and not line.startswith("Picked up JAVA_TOOL_OPTIONS:")]
            result["tools"][name] = {"path": path, "exit_code": p.returncode,
                                     "version": lines[0] if lines else None}
        except (OSError, subprocess.SubprocessError, IndexError) as e:
            result["tools"][name] = {"error": str(e)}
    destination = ROOT / ".runs/setup/doctor.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
