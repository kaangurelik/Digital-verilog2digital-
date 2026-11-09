# Third-party notice and source inventory

This inventory lists the Windows package dependencies, their license notices and available sources. It does not establish that every combined distribution condition is satisfied.

The machine-readable inventory is `scripts/release-licenses.json` in the source checkout and `licenses/inventory.json` in the portable package. It binds each copied notice to its source, size and SHA-256, and records exact Java coordinates, native package versions, model revisions and upstream retrieval URLs. The package builder copies enumerated inputs to their recorded package targets and refuses a VC runtime or OSS CAD companion DLL whose bytes differ from its inventory hash. Rebuilding elsewhere requires those inputs or hash-checked retrieval from the recorded sources. Paths under `collected/` are enumerated explicitly in the inventory. Extra investigation files in that directory are not automatically package inputs.

## Java adapter and Digital

The adapter build's Maven Shade output identifies **21 runtime dependencies**. The audit checked their local `.tools/m2` JARs and followed parent POMs for inherited license declarations. The merged adapter JAR has only one `META-INF/LICENSE`; neither that entry nor the embedded Maven properties provide a complete dependency inventory. In particular, XMLPull, the three EMF JARs and JSpecify are absent from its Maven-properties inventory.

| Runtime dependency | Exact version | Recorded license evidence |
|---|---|---|
| `de.neemann.digital:digital` | `1.0-SNAPSHOT`, upstream `ad6d68fe2e0277cad02beaa7f2a87e366a971b39` plus local modifications | Workspace GPLv3 `LICENSE`; modified source must remain identifiable |
| `com.thoughtworks.xstream:xstream` | `1.4.20` | BSD-3-Clause parent POM and [tagged upstream license](https://github.com/x-stream/xstream/blob/XSTREAM_1_4_20/LICENSE.txt) |
| `io.github.x-stream:mxparser` | `1.2.2` | Indiana University Extreme! Lab license, preserved from the exact JAR |
| `xmlpull:xmlpull` | `1.1.3.1` | [Official public-domain API declaration](https://www.xmlpull.org/v1/download/unpacked/LICENSE.txt); this JAR contains API classes, not LGPL conformance tests |
| `org.slf4j:slf4j-api`, `org.slf4j:slf4j-simple` | `1.7.24` each | MIT parent POM and [tagged upstream license](https://github.com/qos-ch/slf4j/blob/v_1.7.24/LICENSE.txt) |
| `org.json:json` | `20220924` | [This exact tag's public-domain declaration](https://github.com/stleary/JSON-java/blob/20220924/LICENSE); do not substitute another release's terms |
| `org.eclipse.elk:org.eclipse.elk.alg.layered`, `.core`, `.graph`, `.alg.common` | `0.12.0` each | EPL-2.0 parent POMs, each original `about.html`, versioned source JARs and the complete EPL text |
| `org.eclipse.emf:org.eclipse.emf.common`, `.ecore`, `.ecore.xmi` | `2.12.0` each | EPL-1.0 POMs, each original `about.html`, versioned source JARs and the complete EPL text |
| `org.eclipse.xtext:org.eclipse.xtext.xbase.lib` | `2.36.0` | EPL-2.0 parent POM, source JAR and full license text |
| `com.google.guava:guava` | `33.7.1-jre` | Apache-2.0 inherited POM and exact JAR/source license |
| `com.google.guava:failureaccess` | `1.0.3` | Apache-2.0 inherited POM and exact JAR/source license |
| `com.google.guava:listenablefuture` | `9999.0-empty-to-avoid-conflict-with-guava` | Apache-2.0 inherited POM; the JAR has zero classes and is a metadata-only conflict-avoidance placeholder |
| `org.jspecify:jspecify` | `1.0.1` | Apache-2.0 POM, source JAR and full license text |
| `com.google.errorprone:error_prone_annotations` | `2.50.0` | Apache-2.0 POM, source JAR and full license text |
| `com.google.j2objc:j2objc-annotations` | `3.1` | Apache-2.0 POM, source JAR and full license text |

Every row has separate component-specific notice paths in the JSON. Eighteen source JARs were retrieved as data from canonical Maven Central URLs, hashed, and never executed. XMLPull's exact source classifier returned 404; its official API declaration is retained, but exact source recovery remains open. The empty ListenableFuture classifier also returned 404 and has no classes to reconstruct. Digital's corresponding source is the modified workspace, not merely the upstream revision. JUnit, FOP and other test-only dependencies are not represented as shaded runtime dependencies.

The GPLv3/EPL-1.0/EPL-2.0 mixture is an observed fact, not a compatibility conclusion. Before any external binary distribution, retain the actual modified source and build configuration and determine the applicable source-delivery and combined-work requirements. 

## CPython and Python packages

The interpreter is CPython **3.14.0 x64**. The official `python.3.14.0.nupkg` archive has SHA-256 `620fb3527428fb354f093b0b8b634dfb8e3023115df68608fba7e91db69b4f4d`. The local runtime's `LICENSE.txt` includes CPython and bundled notices and is collected. Preserve the exact runtime license when extracting a private interpreter; exclude global `Lib/site-packages`.

All 36 distributions in `agent/requirements-dev.lock` have their own complete installed `*.dist-info/licenses` directories mapped. The converter and test distributions are attrs 26.1.0, colorama 0.4.6, iniconfig 2.3.0, jsonschema 4.26.0, jsonschema-specifications 2025.9.1, packaging 26.3, pluggy 1.6.0, Pygments 2.21.0, pyslang 11.0.0, pytest 9.1.1, referencing 0.37.0, and rpds-py 2026.6.3. Preserve these directories and package metadata even when omitting test packages from a runtime-only bundle. The packaged runtime closure has **30 distributions**: the converter's attrs, jsonschema, jsonschema-specifications, pyslang, referencing and rpds-py, plus the MCP server's `mcp` 2.3.0 and the 23 distributions it requires on Windows. `scripts/package.py` derives that closure from `agent/pyproject.toml` with installed metadata, including the `pyjwt[crypto]` extra and the Windows-only `pywin32` marker. It refuses a build whose closure, lock pins or `.pth` startup hooks differ from the reviewed set. Pip is not needed for the portable environment's operation.

| MCP runtime distribution | Version | Declared license | Preserved dist-info files |
|---|---|---|---|
| annotated-types | 0.8.0 | MIT | `LICENSE` |
| anyio | 4.15.1 | MIT | `LICENSE` |
| cffi | 2.1.1 | MIT-0 | `LICENSE` |
| click | 8.5.0 | BSD-3-Clause | `LICENSE.txt` |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | `LICENSE`, `LICENSE.APACHE`, `LICENSE.BSD`; SBOMs `sbom.json` and `cryptography-rust.cyclonedx.json` |
| h11 | 0.16.0 | MIT (`License` field) | `LICENSE.txt` |
| httpcore2 | 2.16.0 | BSD-3-Clause | `LICENSE.md` |
| httpx2 | 2.16.0 | BSD-3-Clause | `LICENSE.md` |
| idna | 3.20 | BSD-3-Clause | `LICENSE.md` |
| mcp | 2.3.0 | MIT (`License` field) | `LICENSE` |
| mcp-types | 2.3.0 | MIT (`License` field) | `LICENSE` |
| opentelemetry-api | 1.45.2 | Apache-2.0 | `LICENSE` |
| pycparser | 3.12 | BSD-3-Clause | `LICENSE` |
| pydantic | 2.14.2 | MIT | `LICENSE` |
| pydantic_core | 2.50.2 | MIT | `LICENSE`; SBOM `pydantic-core.cyclonedx.json` |
| PyJWT | 2.15.1 | MIT | `LICENSE`, `AUTHORS.rst` |
| python-multipart | 0.0.32 | Apache-2.0 | `LICENSE.txt` |
| pywin32 | 312 | PSF (`License` field) | eight files under `win32/`, `com/`, `pythonwin/`, `adodbapi/` and `isapi/`, described below |
| sse-starlette | 3.6.0 | BSD-3-Clause | `LICENSE`, `AUTHORS` |
| starlette | 1.10.0 | BSD-3-Clause | `LICENSE.md` |
| truststore | 0.10.5 | MIT | `LICENSE` |
| typing_extensions | 4.16.0 | PSF-2.0 | `LICENSE` |
| typing-inspection | 0.4.4 | MIT | `LICENSE` |
| uvicorn | 0.54.0 | BSD-3-Clause | `LICENSE.md` |

Declared licenses are each distribution's `License-Expression`, or its `License` field where it gives no expression. Every listed file is copied byte for byte from the installed `licenses/` or `sboms/` directory to `licenses/python/<distribution>/<dist-info>/`; the SBOMs are recorded as provenance, not as notices.

The `cryptography` wheel's `LICENSE` offers its own code under either the Apache-2.0 or the BSD-3-Clause text beside it. Its `_rust.pyd` statically links OpenSSL 4.0.3: the shipped `sbom.json` records that `no-shared` build, and the binary embeds the OpenSSL version string and imports no OpenSSL DLL. The wheel contains no OpenSSL license or copyright file, so none is staged. The inventory records the upstream archive URL and SHA-256 from the SBOM instead; the OpenSSL notice remains to be collected from that archive. Its CycloneDX SBOM also lists 39 Rust crates linked into the same extension with per-crate license expressions. `pydantic_core` lists 101 crates the same way, and the converter's existing rpds-py dependency lists 17; that SBOM is now staged as provenance too. None of these wheels ships the individual crate license texts.

cffi declares MIT-0 for its own code, and its `LICENSE` defers to notices stated elsewhere in the source. `_cffi_backend` imports no libffi DLL, so libffi is linked into it, but the wheel identifies neither the libffi version nor its license. `typing_extensions` carries the PSF-2.0 text with Python's license history.

pywin32 declares `License: PSF`, but its eight license files differ. They contain Mark Hammond's BSD-style terms for `win32` and Pythonwin, Greg Stein and Mark Hammond's terms for COM, the Scintilla/SciTE notice, Python's license history for Pythonwin's IDLE extensions, LGPL-2.1 for `adodbapi`, MIT for Microsoft's MAPI stub library and an ISAPI copyright statement. The complete installed distribution is packaged, including components the MCP server does not import (Pythonwin, COM, `adodbapi`, ISAPI and `PyWin32.chm`). All eight files are kept without a combined-license conclusion. Its `pywin32.pth` startup hook and `pywin32_system32` DLLs are packaged unchanged. The private venv's isolation probe requires `pywintypes314.dll` to load from the package.

`pyslang.cp314-win_amd64.pyd` also imports Microsoft C++ runtime DLLs, including `MSVCP140_ATOMIC_WAIT.dll`; CPython's own DLL set alone is insufficient. The inventory records the local x64 redistribution directory `VC/Redist/MSVC/14.44.35112/x64/Microsoft.VC143.CRT` from Visual Studio 2022 Build Tools and hashes all ten files. It preserves the installed Redist pointer and third-party notices, plus Microsoft's [redistribution list](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution). Applicability of edition-specific license conditions is not established merely by locating those files. Windows system DLLs are not copied into the package as project-owned redistributables.

## Native GUI, JDK and HDL tools

The GUI requires 17 private DLLs. Installed MSYS2 package metadata and corresponding license directories identify the exact owners:

| Package | Installed version |
|---|---|
| wxWidgets common/MSW libraries | `3.2.10-2` |
| GCC runtime | `16.1.0-5` |
| libwinpthread | `14.0.0.r98.g19f5121a2-1` |
| PCRE2 | `10.47-1` |
| zlib | `1.3.2-2` |
| xz/liblzma | `5.8.3-1` |
| libtiff | `4.7.1-1` |
| zstd | `1.5.7-2` |
| libwebp/sharpyuv | `1.6.0-1` |
| Lerc | `4.1.0-1` |
| libjpeg-turbo | `3.1.4.1-3` |
| JBIG-KIT | `2.1-5` |
| libdeflate | `1.25-1` |
| libpng | `1.6.58-1` |
| nlohmann JSON headers | `3.12.0-1` |

The manifest preserves package-declared license expressions without flattening them into one application license. This includes GCC's runtime exception, wxWidgets' exception, and JBIG-KIT's GPL declaration. All files under the matching `C:/msys64/ucrt64/share/licenses/` directories are retained, including supplementary copyright and assembly notices. Keep the GUI's UCRT64 DLLs separate from OSS CAD Suite's incompatible DLLs with identical names.

Temurin JDK `17.0.20.1+1` retains its complete `legal/` tree. OSS CAD Suite `2026-09-05` retains all 46 files under its `license/` directory. The nine additional runtime DLLs actually selected by `HDL_FILES` now have exact Fedora package-member matches and separate notice mappings; another distribution's similarly named file was not substituted as binary provenance. Digital's supplied circuit library and stock application remain associated with the repository license and modified source snapshot.

The additional HDL dependency `lib/libbz2-1.dll` is bzip2/libbzip2 `1.0.8` and exactly matches the DLL in Fedora's `mingw64-bzip2-1.0.8-16.fc43` package (113,950 bytes, SHA-256 `8de14c88d70cbac79bc8e3fdbc9859f7abc1d397a42533d1bddf1d4268136ea7`). The [release-tagged OSS CAD Dockerfile](https://github.com/YosysHQ/oss-cad-suite-build/blob/695ed6ce3a61d10332578d96eb40371dc94dfb3c/docker/cross-windows-x64/Dockerfile) installs Fedora 43's `mingw64-bzip2`; its Windows packaging script copies those DLLs into `lib/`. The [exact Fedora package](https://kojipkgs.fedoraproject.org/packages/mingw-bzip2/1.0.8/16.fc43/noarch/mingw64-bzip2-1.0.8-16.fc43.noarch.rpm) supplies Julian Seward's copyright and conditions in `COPYING`, which is byte-identical to `LICENSE` in the [official bzip2 1.0.8 source archive](https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz). That source archive was checked against the upstream SHA-512 index. The notice, source archive, Fedora package and recipe evidence are staged under `licenses/oss-cad-suite/bzip2/`; exact mappings are recorded in `oss_cad_runtime_dependencies`. The Fedora RPM was retrieved over HTTPS and its DLL/notice bytes compared; its OpenPGP signature was not independently verified. Fedora's source RPM is referenced, while the included source archive remains the upstream release.

The other eight runtime DLLs match these exact Fedora packages. All package-provided notice files listed below are copied byte for byte into `licenses/oss-cad-suite/runtime/`, alongside each package archive and its hash/metadata record.

| Bundled DLL | Exact matching package | Preserved notice |
| --- | --- | --- |
| `libstdc++-6.dll` | [mingw64-libstdc++ 15.2.1-2.fc43](https://kojipkgs.fedoraproject.org/packages/mingw-gcc/15.2.1/2.fc43/x86_64/mingw64-libstdc++-15.2.1-2.fc43.x86_64.rpm) | `COPYING`, `COPYING.LIB`, `COPYING.RUNTIME`, `COPYING3`, `COPYING3.LIB` |
| `libgcc_s_seh-1.dll` | [mingw64-libgcc 15.2.1-2.fc43](https://kojipkgs.fedoraproject.org/packages/mingw-gcc/15.2.1/2.fc43/x86_64/mingw64-libgcc-15.2.1-2.fc43.x86_64.rpm) | Same five GCC notice files |
| `libwinpthread-1.dll` | [mingw64-winpthreads 13.0.0-2.fc43](https://kojipkgs.fedoraproject.org/packages/mingw-winpthreads/13.0.0/2.fc43/noarch/mingw64-winpthreads-13.0.0-2.fc43.noarch.rpm) | `COPYING` |
| `libreadline8.dll` | [mingw64-readline 8.3-2.fc43](https://kojipkgs.fedoraproject.org/packages/mingw-readline/8.3/2.fc43/noarch/mingw64-readline-8.3-2.fc43.noarch.rpm) | `COPYING` |
| `libtermcap-0.dll` | [mingw64-termcap 1.3.1-39.fc43](https://kojipkgs.fedoraproject.org/packages/mingw-termcap/1.3.1/39.fc43/noarch/mingw64-termcap-1.3.1-39.fc43.noarch.rpm) | `COPYING` |
| `tcl86.dll` | [mingw64-tcl 8.6.15-3.fc43](https://kojipkgs.fedoraproject.org/packages/mingw-tcl/8.6.15/3.fc43/noarch/mingw64-tcl-8.6.15-3.fc43.noarch.rpm) | `license.terms` |
| `libffi-8.dll` | [mingw64-libffi 3.5.1-2.fc43](https://kojipkgs.fedoraproject.org/packages/mingw-libffi/3.5.1/2.fc43/noarch/mingw64-libffi-3.5.1-2.fc43.noarch.rpm) | `LICENSE` |
| `zlib1.dll` | [mingw64-zlib 1.3.2-1.fc43](https://kojipkgs.fedoraproject.org/packages/mingw-zlib/1.3.2/1.fc43/noarch/mingw64-zlib-1.3.2-1.fc43.noarch.rpm) | Complete leading copyright/terms comment from its packaged `zlib.h`; original header retained |

Each binary RPM identifies its exact source RPM; those source URLs and versions are recorded rather than replaced with another distribution's sources. Full Fedora source RPMs are not included or rebuilt, and binary RPM signatures have not been independently verified. GCC's full package license expression and runtime exceptions are retained. Readline's package metadata declares `GPL-2.0-or-later`, while its included `COPYING` is GNU GPL version 3; both observations are preserved without inferring a license downgrade or compatibility ruling.

## Optional llama.cpp, models and NVIDIA libraries

The runtime is the pinned `b10809` build at source commit `5266f24da75dc449bd56cbed7addb9c8e4a6a73e`. Each CPU/CUDA archive member and its pinned hash are preserved in the inventory. Both original archives include a standalone LLVM OpenMP notice. Static inspection of each exact `llama.exe` additionally recovered five embedded license sections: LLVM OpenMP, llama.cpp, jsonhpp, BoringSSL and cpp-httplib. Their byte offsets, original executable hashes and exact extracted text are recorded; no executable was launched to recover them.

The [pinned llama.cpp source license](https://github.com/ggml-org/llama.cpp/blob/5266f24da75dc449bd56cbed7addb9c8e4a6a73e/LICENSE) and vendored licenses were retrieved from the same commit. Additional embedded source notices for hash functions, stb, miniaudio, subprocess helpers and llamafile SGEMM are retained as source evidence. The pinned CMake file selects BoringSSL `0.20260903.0`; canonical tag/commit metadata and its license were collected. This establishes the declared default, while the exact upstream release build's overridden flags and complete embedded browser/UI dependency attribution remain unverified. A source tree search alone does not prove which optional dependency was compiled into every utility shipped in the full archive.

The exact official model repositories and revisions are:

| Weights | Repository revision | License input |
|---|---|---|
| Qwen3-4B Q4_K_M and Q5_K_M | `Qwen/Qwen3-4B-GGUF` at `bc640142c66e1fdd12af0bd68f40445458f3869b` | [Pinned LICENSE](https://huggingface.co/Qwen/Qwen3-4B-GGUF/blob/bc640142c66e1fdd12af0bd68f40445458f3869b/LICENSE) |
| Qwen3-14B Q4_K_M | `Qwen/Qwen3-14B-GGUF` at `530227a7d994db8eca5ab5ced2fb692b614357fd` | [Pinned LICENSE](https://huggingface.co/Qwen/Qwen3-14B-GGUF/blob/530227a7d994db8eca5ab5ced2fb692b614357fd/LICENSE) |

Both retrieved license files contain Apache-2.0 text and have SHA-256 `5de36594c10839788a8c589443a8ef9d8b8d17c65a1b5807206ae037fc36c6bd`. Their original README/model cards and exact GGUF hashes are retained separately; this does not substitute one quantization's bytes for another.

The pinned NVIDIA ZIP contains exactly `cublas64_12.dll`, `cublasLt64_12.dll` and `cudart64_12.dll`. NVIDIA's official CUDA 12.4.1 redistribution index identifies cuBLAS `12.4.5.8` and cudart `12.4.127`. The small official cudart archive was downloaded, verified against that index's SHA-256, and its original LICENSE extracted. Its `cudart64_12.dll` hash exactly matches the member in the pinned llama CUDA package. The full official cuBLAS archive was not independently downloaded or compared; its index identity and existing llama archive/member hashes are recorded distinctly.

The [archived CUDA 12.4.1 EULA](https://docs.nvidia.com/cuda/archive/12.4.1/eula/index.html) lists versioned forms of these runtime/BLAS files among its distributable components, subject to the agreement's conditions. The complete archived terms and versioned cudart license are retained. Current rolling component-license URLs are stored as supplemental evidence only. The NVIDIA driver remains a system prerequisite; no driver redistribution is claimed here. Notice collection is not a finding that a particular future publication satisfies the NVIDIA or Microsoft agreements.

## Source availability

The portable package includes `sources/verilog2digital-source.zip`, containing the application and modified Digital sources, build scripts and configuration, tests and user documentation. Generated build files are excluded. Source availability gaps remain for XMLPull and the referenced Fedora source RPMs. Attribution of optional llama.cpp build/UI dependencies and applicable redistribution conditions also remain unresolved. So does attribution for OpenSSL and the Rust crates linked into the cryptography, pydantic_core and rpds-py wheels, and for libffi in cffi: the wheels identify those components but do not ship their notices. Until their license texts are collected and staged, `scripts/package.py --archive` refuses to build the distributable archive, and a package folder built without `--archive` warns that it must not be distributed. The Python wheels are packaged as installed; their upstream source distributions were not retrieved.
