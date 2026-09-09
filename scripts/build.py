"""Build the existing Digital project and the isolated application modules."""
import argparse
from pathlib import Path
from toolchain import ROOT, build_runtime_support, executable, maven_args, run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=["digital", "adapter", "gui", "all"])
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--clean", action="store_true", help="Remove selected generated build output before rebuilding")
    args = parser.parse_args()
    build_runtime_support()
    targets = ["digital", "adapter", "gui"] if args.target == "all" else [args.target]
    for target in targets:
        if target == "digital":
            flags = ["-DskipTests"] if args.skip_tests else []
            output = (ROOT / ".runs/digital-build").resolve()
            if not output.is_relative_to(ROOT.resolve()):
                raise ValueError("Digital build output must remain inside the workspace")
            flags.append(f"-Dv2d.build.directory={output}")
            goals = ["clean", "install"] if args.clean else ["install"]
            run(maven_args(*flags, *goals), log="digital-build.log", timeout=1800)
        elif target == "adapter":
            project = ROOT / "digital-adapter"
            if not (project / "target").resolve().is_relative_to(ROOT.resolve()):
                raise ValueError("Adapter build output must remain inside the workspace")
            flags = ["-DskipTests"] if args.skip_tests else []
            goals = ["clean", "verify"] if args.clean else ["verify"]
            run(maven_args(*flags, *goals, project=project), cwd=project, log="adapter-build.log", timeout=900)
        else:
            prefix = Path(executable("g++")).resolve().parents[1]
            output = (ROOT / "gui/build").resolve()
            if not output.is_relative_to(ROOT.resolve()):
                raise ValueError("GUI build output must remain inside the workspace")
            run([executable("cmake"), "-S", str(ROOT / "gui"), "-B", str(output),
                 "-G", "Ninja", f"-DCMAKE_PREFIX_PATH={prefix.as_posix()}",
                 f"-DCMAKE_CXX_COMPILER={executable('g++')}", "-DCMAKE_BUILD_TYPE=Release"], log="gui-configure.log")
            flags = ["--clean-first"] if args.clean else []
            run([executable("cmake"), "--build", str(output), *flags, "--parallel", "2"], log="gui-build.log")


if __name__ == "__main__":
    main()
