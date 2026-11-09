// Own the bootstrap process tree until exit, then launch the GUI independently.
// --mcp keeps owning the tree while the host's protocol pipes pass straight through.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <shellapi.h>
#include <nlohmann/json.hpp>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cwctype>
#include <filesystem>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace v2d::portable {
using Json = nlohmann::json;
using Path = std::filesystem::path;
using Clock = std::chrono::steady_clock;

class Handle {
public:
    explicit Handle(HANDLE value = nullptr) : value_(value) {}
    ~Handle() { Reset(); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    Handle(Handle&& other) noexcept : value_(other.Release()) {}
    Handle& operator=(Handle&& other) noexcept { Reset(other.Release()); return *this; }
    HANDLE Get() const { return value_; }
    explicit operator bool() const { return value_ && value_ != INVALID_HANDLE_VALUE; }
    HANDLE Release() { auto result = value_; value_ = nullptr; return result; }
    void Reset(HANDLE value = nullptr) { if (*this) CloseHandle(value_); value_ = value; }
private:
    HANDLE value_;
};

void Check(BOOL value, const char* operation) {
    if (!value) throw std::runtime_error(std::string(operation) + " failed (Windows error " + std::to_string(GetLastError()) + ")");
}

std::wstring Wide(const std::string& value) {
    if (value.empty()) return {};
    if (value.size() > 16 * 1024 * 1024) throw std::runtime_error("UTF-8 text exceeds its limit");
    int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0);
    if (!size) throw std::runtime_error("Bootstrap returned invalid UTF-8");
    std::wstring result(size, L'\0');
    Check(MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), size), "Decode bootstrap text");
    return result;
}

void Valid(const std::wstring& value) {
    if (value.size() >= 32767 || value.find(L'\0') != std::wstring::npos)
        throw std::runtime_error("A path or argument exceeds Windows limits");
    if (!value.empty() && !WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr))
        throw std::runtime_error("Paths and arguments must contain valid Unicode");
}

std::wstring Quote(const std::wstring& value) {
    Valid(value);
    std::wstring result = L"\"";
    std::size_t slashes = 0;
    for (auto c : value) {
        if (c == L'\\') { ++slashes; continue; }
        result.append(c == L'"' ? slashes * 2 + 1 : slashes, L'\\');
        slashes = 0;
        result += c;
    }
    result.append(slashes * 2, L'\\');
    return result + L'"';
}

std::wstring CommandLine(const std::vector<std::wstring>& args) {
    if (args.empty() || args.size() > 4096) throw std::runtime_error("Invalid argument count");
    std::wstring result;
    for (const auto& arg : args) {
        if (!result.empty()) result += L' ';
        result += Quote(arg);
        if (result.size() >= 32767) throw std::runtime_error("Command line exceeds Windows limits");
    }
    return result;
}

std::wstring Lower(std::wstring value) {
    std::transform(value.begin(), value.end(), value.begin(), [](wchar_t c) { return static_cast<wchar_t>(std::towlower(c)); });
    return value;
}

Path Absolute(const Path& value) {
    auto result = std::filesystem::absolute(value).lexically_normal();
    Valid(result.native());
    return result;
}

// The bootstrap reports Path.resolve() results: junctions, substituted and mapped
// drives and short names appear in their final form, which the launcher must match.
Path Final(const Path& value) {
    const auto path = Absolute(value);
    Handle handle(CreateFileW(path.c_str(), 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                              OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr));
    if (!handle) return path;
    std::vector<wchar_t> buffer(32768);
    const auto count = GetFinalPathNameByHandleW(handle.Get(), buffer.data(), static_cast<DWORD>(buffer.size()), FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (!count || count >= buffer.size()) return path;
    std::wstring text(buffer.data(), count);
    if (text.rfind(L"\\\\?\\UNC\\", 0) == 0) text = L"\\\\" + text.substr(8);
    else if (text.rfind(L"\\\\?\\", 0) == 0) text = text.substr(4);
    return Absolute(text);
}

bool SamePath(const Path& first, const Path& second) {
    auto a = Final(first).native(), b = Final(second).native();
    return CompareStringOrdinal(a.data(), static_cast<int>(a.size()), b.data(), static_cast<int>(b.size()), TRUE) == CSTR_EQUAL;
}

Path Executable() {
    std::vector<wchar_t> buffer(32768);
    auto count = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (!count || count >= buffer.size()) throw std::runtime_error("Cannot locate the portable launcher");
    return Absolute(std::wstring(buffer.data(), count));
}

using Variables = std::map<std::wstring, std::wstring>;
Variables Environment(const Path& root) {
    Variables result;
    auto* inherited = GetEnvironmentStringsW();
    if (!inherited) throw std::runtime_error("Cannot read the Windows environment");
    for (const wchar_t* at = inherited; *at; at += std::wcslen(at) + 1) {
        const std::wstring item(at);
        auto split = item.find(L'=', item.front() == L'=' ? 1 : 0);
        if (split != std::wstring::npos) result[Lower(item.substr(0, split))] = item.substr(split + 1);
    }
    FreeEnvironmentStringsW(inherited);
    // Match portable_bootstrap.py: the desktop inherits this environment, not the bootstrap's.
    const std::set<std::wstring> removed{L"virtual_env", L"java_home", L"java_tool_options", L"_java_options",
                                         L"jdk_java_options", L"tcl_library", L"tk_library"};
    for (auto it = result.begin(); it != result.end();) {
        const auto& key = it->first;
        const bool internal = key.rfind(L"v2d_", 0) == 0 && key != L"v2d_job_root" && key != L"v2d_native_tools_root";
        it = key.rfind(L"python", 0) == 0 || internal || removed.count(key) ? result.erase(it) : std::next(it);
    }
    result[L"pythonutf8"] = L"1";
    result[L"pythonnousersite"] = L"1";
    result[L"v2d_root"] = root.native();
    return result;
}

std::vector<wchar_t> EnvironmentBlock(const Variables& values) {
    std::vector<wchar_t> result;
    for (const auto& [name, value] : values) {
        Valid(value);
        std::wstring item = name + L'=' + value;
        result.insert(result.end(), item.begin(), item.end());
        result.push_back(L'\0');
        if (result.size() > 1024 * 1024) throw std::runtime_error("Environment exceeds its limit");
    }
    result.push_back(L'\0');
    if (values.empty()) result.push_back(L'\0');
    return result;
}

struct Options {
    enum class Mode { Gui, Smoke, Doctor, Diagnostics, Cli, Mcp } mode = Mode::Gui;
    std::optional<Path> project;
    std::vector<std::wstring> cli;  // The v2d command for --cli, or the `v2d mcp` arguments for --mcp.
};

Options ParseOptions(const std::vector<std::wstring>& args) {
    Options options;
    for (std::size_t i = 0; i < args.size(); ++i) {
        Valid(args[i]);
        if (i == 0 && ((args[i] == L"--cli" && args.size() > 1) || args[i] == L"--mcp")) {
            options.mode = args[i] == L"--cli" ? Options::Mode::Cli : Options::Mode::Mcp;
            options.cli.assign(args.begin() + 1, args.end());
            for (const auto& arg : options.cli) Valid(arg);
            return options;
        }
        if (args[i] == L"--project" && i + 1 < args.size() && !options.project) {
            options.project = Absolute(args[++i]);
        } else if ((args[i] == L"--smoke-test" || args[i] == L"--doctor" || args[i] == L"--diagnostics") && options.mode == Options::Mode::Gui) {
            options.mode = args[i] == L"--smoke-test" ? Options::Mode::Smoke : args[i] == L"--doctor" ? Options::Mode::Doctor : Options::Mode::Diagnostics;
        } else throw std::runtime_error("Use --project <file>, --smoke-test, --doctor, --diagnostics, --cli <arguments>, or --mcp [arguments]");
    }
    if (options.project && options.mode != Options::Mode::Gui && options.mode != Options::Mode::Smoke)
        throw std::runtime_error("--project is only valid when opening the desktop");
    return options;
}

struct Plan { Path executable; Variables environment; };
Plan ParsePlan(const std::string& bytes, const Path& root) {
    if (bytes.empty() || bytes.size() > 65536 || bytes.compare(0, 3, "\xef\xbb\xbf") == 0)
        throw std::runtime_error("Bootstrap launch plan exceeds its limit or has an invalid encoding");
    Wide(bytes);
    std::vector<std::set<std::string>> keys;
    std::size_t nodes = 0;
    auto callback = [&](int depth, Json::parse_event_t event, Json& value) {
        if (depth > 8 || ++nodes > 256) throw std::runtime_error("Bootstrap launch plan is too complex");
        if (event == Json::parse_event_t::object_start) keys.emplace_back();
        else if (event == Json::parse_event_t::object_end) keys.pop_back();
        else if (event == Json::parse_event_t::key && !keys.back().insert(value.get<std::string>()).second)
            throw std::runtime_error("Bootstrap launch plan contains duplicate keys");
        return true;
    };
    auto value = Json::parse(bytes, callback);
    if (!value.is_object() || value.size() != 4 || !value.contains("schema_version") || !value["schema_version"].is_number_integer()
        || value["schema_version"] != 1 || !value.contains("root") || !value["root"].is_string()
        || !value.contains("gui") || !value["gui"].is_object() || value["gui"].size() != 1
        || !value["gui"].contains("executable") || !value["gui"]["executable"].is_string()
        || !value.contains("environment") || !value["environment"].is_object()) throw std::runtime_error("Invalid bootstrap launch plan");
    auto planRoot = Path(Wide(value["root"].get<std::string>()));
    auto executable = Path(Wide(value["gui"]["executable"].get<std::string>()));
    if (!planRoot.is_absolute() || !SamePath(planRoot, root) || !executable.is_absolute()
        || !SamePath(executable, root / L"gui/build/v2d-gui.exe")) throw std::runtime_error("Bootstrap launch plan names another application");
    Plan plan{Absolute(executable), Environment(root)};
    const std::set<std::string> allowed{"V2D_ROOT", "V2D_JOB_ROOT", "V2D_NATIVE_TOOLS_ROOT", "PATH"};
    for (auto it = value["environment"].begin(); it != value["environment"].end(); ++it) {
        if (!allowed.count(it.key()) || !it.value().is_string()) throw std::runtime_error("Unexpected bootstrap environment setting");
        auto text = Wide(it.value().get<std::string>());
        Valid(text);
        if (it.key() == "V2D_ROOT") {
            if (!Path(text).is_absolute() || !SamePath(text, root)) throw std::runtime_error("Bootstrap root does not match this application");
        } else if (it.key() != "PATH") {
            if (!Path(text).is_absolute() || std::any_of(text.begin(), text.end(), [](wchar_t c) { return c > 127; }))
                throw std::runtime_error("Native tool and job directories must have absolute ASCII paths");
        }
        plan.environment[Lower(Wide(it.key()))] = text;
    }
    for (const auto* key : {"V2D_ROOT", "V2D_JOB_ROOT", "PATH"})
        if (!value["environment"].contains(key)) throw std::runtime_error("Bootstrap environment is incomplete");
    return plan;
}

struct Pipe {
    Handle read, write;
    void Open() {
        SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
        HANDLE r = nullptr, w = nullptr;
        Check(CreatePipe(&r, &w, &security, 0), "Create bootstrap output pipe");
        read.Reset(r); write.Reset(w);
        Check(SetHandleInformation(read.Get(), HANDLE_FLAG_INHERIT, 0), "Protect bootstrap output pipe");
    }
};

DWORD Active(HANDLE job) {
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION value{};
    Check(QueryInformationJobObject(job, JobObjectBasicAccountingInformation, &value, sizeof(value), nullptr), "Read bootstrap process accounting");
    return value.ActiveProcesses;
}

Handle Ownership(ULONGLONG memory = 0) {
    Handle job(CreateJobObjectW(nullptr, nullptr));
    Check(static_cast<bool>(job), "Create bootstrap ownership");
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
    limits.BasicLimitInformation.ActiveProcessLimit = 128;
    if (memory) { limits.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY; limits.JobMemoryLimit = memory; }
    Check(SetInformationJobObject(job.Get(), JobObjectExtendedLimitInformation, &limits, sizeof(limits)), "Set bootstrap ownership limits");
    return job;
}

void Stop(HANDLE job) {
    TerminateJobObject(job, ERROR_CANCELLED);
    const auto deadline = Clock::now() + std::chrono::seconds(5);
    while (Active(job) && Clock::now() < deadline) Sleep(10);
}

// The job owns the child from its first instruction; it inherits only the three standard handles.
PROCESS_INFORMATION StartOwned(const std::vector<std::wstring>& args, const Path& root, const Variables& env,
                               HANDLE job, HANDLE input, HANDLE output, HANDLE error) {
    SIZE_T size = 0; InitializeProcThreadAttributeList(nullptr, 2, 0, &size);
    std::vector<unsigned char> storage(size);
    auto attributes = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(storage.data());
    Check(InitializeProcThreadAttributeList(attributes, 2, 0, &size), "Initialize bootstrap attributes");
    struct Attributes { LPPROC_THREAD_ATTRIBUTE_LIST value; ~Attributes() { DeleteProcThreadAttributeList(value); } } cleanup{attributes};
    HANDLE jobs[]{job}, handles[]{input, output, error};
    Check(UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, sizeof(jobs), nullptr, nullptr), "Own bootstrap before execution");
    Check(UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, sizeof(handles), nullptr, nullptr), "Restrict bootstrap handles");
    STARTUPINFOEXW startup{}; startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    startup.StartupInfo.wShowWindow = SW_HIDE;
    startup.StartupInfo.hStdInput = input; startup.StartupInfo.hStdOutput = output; startup.StartupInfo.hStdError = error;
    startup.lpAttributeList = attributes;
    PROCESS_INFORMATION info{}; auto command = CommandLine(args); auto block = EnvironmentBlock(env);
    Check(CreateProcessW(args.front().c_str(), command.data(), nullptr, nullptr, TRUE,
                         CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
                         block.data(), root.c_str(), &startup.StartupInfo, &info), "Start private Python bootstrap");
    return info;
}

struct Capture { std::string output, error; DWORD exit_code = 0; DWORD remaining_processes = 0; };

void ReadAvailable(HANDLE pipe, std::string& data, std::size_t maximum, bool& ended) {
    if (ended) return;
    DWORD available = 0;
    if (!PeekNamedPipe(pipe, nullptr, 0, nullptr, &available, nullptr)) {
        if (GetLastError() == ERROR_BROKEN_PIPE) { ended = true; return; }
        Check(FALSE, "Read bootstrap pipe state");
    }
    // Bounded work per poll keeps the deadline live during continuous output.
    available = std::min<DWORD>(available, 65536);
    while (available) {
        char buffer[8192]; DWORD count = 0;
        Check(ReadFile(pipe, buffer, std::min<DWORD>(available, sizeof(buffer)), &count, nullptr), "Read bootstrap output");
        if (!count) { ended = true; return; }
        if (data.size() + count > maximum) throw std::runtime_error("Bootstrap output exceeded its limit");
        data.append(buffer, count);
        available -= count;
    }
}

Capture RunOwned(const std::vector<std::wstring>& args, const Path& root, Variables env,
                 std::chrono::milliseconds timeout, bool prepare) {
    auto job = Ownership(prepare ? 512ull * 1024 * 1024 : 0);
    Pipe out, err; out.Open(); err.Open();
    SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
    Handle input(CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_EXISTING, 0, nullptr));
    Check(static_cast<bool>(input), "Open bootstrap input");
    auto info = StartOwned(args, root, env, job.Get(), input.Get(), out.write.Get(), err.write.Get());
    Handle process(info.hProcess), thread(info.hThread);
    out.write.Reset(); err.write.Reset();
    Capture result; bool outEnded = false, errEnded = false;
    auto deadline = Clock::now() + timeout;
    std::optional<Clock::time_point> exited;
    try {
        for (;;) {
            ReadAvailable(out.read.Get(), result.output, prepare ? 65536 : 16 * 1024 * 1024, outEnded);
            ReadAvailable(err.read.Get(), result.error, 1024 * 1024, errEnded);
            DWORD wait = WaitForSingleObject(process.Get(), 0);
            if (wait == WAIT_OBJECT_0) {
                if (!exited) { exited = Clock::now(); Check(GetExitCodeProcess(process.Get(), &result.exit_code), "Read bootstrap exit code"); }
                result.remaining_processes = Active(job.Get());
                if (!result.remaining_processes && outEnded && errEnded) break;
                if (result.remaining_processes && Clock::now() - *exited > std::chrono::seconds(1))
                    throw std::runtime_error("Bootstrap exited with a live descendant");
            } else if (wait != WAIT_TIMEOUT) Check(FALSE, "Wait for bootstrap");
            if (Clock::now() >= deadline) throw std::runtime_error("Portable bootstrap exceeded its time limit");
            Sleep(10);
        }
    } catch (...) {
        Stop(job.Get());
        throw;
    }
    return result;
}

// MCP hosts own the protocol pipes. The server tree receives them unchanged, so no
// launcher buffer, text conversion or deadline sits between the host and the server.
struct Streams {
    HANDLE input, output, error;
    static Streams Current() { return {GetStdHandle(STD_INPUT_HANDLE), GetStdHandle(STD_OUTPUT_HANDLE), GetStdHandle(STD_ERROR_HANDLE)}; }
};

bool Connected(HANDLE handle) {
    if (!handle || handle == INVALID_HANDLE_VALUE) return false;
    SetLastError(NO_ERROR);
    return GetFileType(handle) != FILE_TYPE_UNKNOWN || GetLastError() == NO_ERROR;
}

Handle Duplicate(HANDLE source, bool inheritable, const char* operation) {
    HANDLE copy = nullptr;
    Check(DuplicateHandle(GetCurrentProcess(), source, GetCurrentProcess(), &copy, 0, inheritable, DUPLICATE_SAME_ACCESS), operation);
    return Handle(copy);
}

// Peeking never consumes protocol bytes: a pipe the host closed reports ERROR_BROKEN_PIPE
// once the server has read everything sent before the close. A peek can wait behind the
// server's own pending read of the same pipe, so it runs on a thread that owns its state.
class InputWatch {
public:
    explicit InputWatch(HANDLE input) : state_(std::make_shared<State>()) {
        state_->closed.Reset(CreateEventW(nullptr, TRUE, FALSE, nullptr));
        Check(static_cast<bool>(state_->closed), "Create MCP input watch");
        if (GetFileType(input) != FILE_TYPE_PIPE) return;  // A file or device ends when the server reads it.
        state_->input = Duplicate(input, false, "Watch MCP input");
        auto owner = std::make_unique<std::shared_ptr<State>>(state_);
        Handle thread(CreateThread(nullptr, 0, Watch, owner.get(), 0, nullptr));
        Check(static_cast<bool>(thread), "Start MCP input watch");
        owner.release();
    }
    ~InputWatch() { state_->stopped = true; }
    InputWatch(const InputWatch&) = delete;
    InputWatch& operator=(const InputWatch&) = delete;
    HANDLE Closed() const { return state_->closed.Get(); }
private:
    struct State { Handle input, closed; std::atomic<bool> stopped{false}; };
    static DWORD WINAPI Watch(void* value) {
        std::unique_ptr<std::shared_ptr<State>> owner(static_cast<std::shared_ptr<State>*>(value));
        auto& state = **owner;
        for (DWORD available = 0; !state.stopped; Sleep(50)) {
            if (PeekNamedPipe(state.input.Get(), nullptr, 0, nullptr, &available, nullptr)) continue;
            const auto error = GetLastError();
            if (error == ERROR_BROKEN_PIPE || error == ERROR_PIPE_NOT_CONNECTED) SetEvent(state.closed.Get());
            break;
        }
        return 0;
    }
    std::shared_ptr<State> state_;
};

// Runs `v2d mcp` through the bootstrap for as long as the host keeps it: no runtime cap.
// The job ends the whole tree when the launcher is terminated, when the server outlives
// its closed input by `grace`, or when the server exits and leaves a descendant behind.
int Serve(const std::vector<std::wstring>& args, const Path& root, const Variables& env, const Streams& host,
          std::chrono::milliseconds grace = std::chrono::seconds(10)) {
    if (!Connected(host.input) || !Connected(host.output))
        throw std::runtime_error("--mcp needs standard input and output connected to an MCP host");
    auto job = Ownership();
    InputWatch watch(host.input);
    Handle input = Duplicate(host.input, true, "Share MCP input"), output = Duplicate(host.output, true, "Share MCP output"), error;
    if (Connected(host.error)) error = Duplicate(host.error, true, "Share MCP log stream");
    else {
        SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
        error.Reset(CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_EXISTING, 0, nullptr));
        Check(static_cast<bool>(error), "Open MCP log sink");
    }
    auto info = StartOwned(args, root, env, job.Get(), input.Get(), output.Get(), error.Get());
    Handle process(info.hProcess), thread(info.hThread);
    input.Reset(); output.Reset(); error.Reset();
    try {
        HANDLE events[]{process.Get(), watch.Closed()};
        const auto signaled = WaitForMultipleObjects(2, events, FALSE, INFINITE);
        if (signaled != WAIT_OBJECT_0 && signaled != WAIT_OBJECT_0 + 1) Check(FALSE, "Wait for MCP server");
        if (signaled == WAIT_OBJECT_0 + 1 && WaitForSingleObject(process.Get(), static_cast<DWORD>(grace.count())) != WAIT_OBJECT_0)
            throw std::runtime_error("MCP server did not exit within " + std::to_string(grace.count()) + " ms after its input closed; its process tree was stopped");
        DWORD code = 0;
        Check(GetExitCodeProcess(process.Get(), &code), "Read MCP server exit code");
        const auto deadline = Clock::now() + std::chrono::seconds(2);
        while (Active(job.Get()) && Clock::now() < deadline) Sleep(10);
        if (Active(job.Get())) throw std::runtime_error("MCP server exited with a live descendant; its process tree was stopped");
        return static_cast<int>(code);
    } catch (...) {
        Stop(job.Get());
        throw;
    }
}

Handle StartGui(const Plan& plan, const Path& root, const Options& options) {
    if (!std::filesystem::is_regular_file(plan.executable)) throw std::runtime_error("The checked GUI executable is missing");
    std::vector<std::wstring> args{plan.executable.native(), L"--root", root.native()};
    if (options.project) { args.push_back(L"--project"); args.push_back(options.project->native()); }
    if (options.mode == Options::Mode::Smoke) args.push_back(L"--smoke-test");
    auto command = CommandLine(args); auto environment = EnvironmentBlock(plan.environment);
    STARTUPINFOW startup{}; startup.cb = sizeof(startup);
    PROCESS_INFORMATION info{};
    Check(CreateProcessW(plan.executable.c_str(), command.data(), nullptr, nullptr, FALSE, CREATE_UNICODE_ENVIRONMENT,
                         environment.data(), root.c_str(), &startup, &info), "Start portable desktop");
    Handle thread(info.hThread);
    return Handle(info.hProcess);
}

bool Output(HANDLE destination, const std::string& bytes) {
    if (!destination || destination == INVALID_HANDLE_VALUE) return false;
    DWORD flags = 0;
    if (!GetHandleInformation(destination, &flags)) return false;
    DWORD console = 0;
    if (GetConsoleMode(destination, &console)) {
        auto text = Wide(bytes); std::size_t at = 0;
        while (at < text.size()) {
            DWORD written = 0;
            if (!WriteConsoleW(destination, text.data() + at, static_cast<DWORD>(std::min<std::size_t>(8192, text.size() - at)), &written, nullptr) || !written) return false;
            at += written;
        }
        return true;
    }
    if (GetFileType(destination) == FILE_TYPE_CHAR) return false; // NUL is not user-visible output.
    std::size_t at = 0;
    while (at < bytes.size()) {
        DWORD written = 0;
        if (!WriteFile(destination, bytes.data() + at, static_cast<DWORD>(std::min<std::size_t>(65536, bytes.size() - at)), &written, nullptr) || !written) return false;
        at += written;
    }
    return true;
}

int Run(const Path& root, const Options& options) {
    auto python = root/L".tools/python-runtime/python.exe", script = root/L"scripts/portable_bootstrap.py";
    if (!std::filesystem::is_regular_file(python) || !std::filesystem::is_regular_file(script))
        throw std::runtime_error("Portable runtime is incomplete; extract the full package before starting it");
    std::vector<std::wstring> args{python.native(), L"-I", L"-X", L"utf8", script.native(), L"--root", root.native()};
    if (options.mode == Options::Mode::Mcp) {
        // The host's working directory resolves relative --allow-dir paths.
        args.push_back(L"--cwd"); args.push_back(Absolute(std::filesystem::current_path()).native());
        args.push_back(L"--mcp"); args.insert(args.end(), options.cli.begin(), options.cli.end());
        return Serve(args, root, Environment(root), Streams::Current());
    }
    const bool prepare = options.mode == Options::Mode::Gui || options.mode == Options::Mode::Smoke;
    if (prepare) args.push_back(L"--prepare");
    else if (options.mode == Options::Mode::Doctor) args.push_back(L"--doctor");
    else if (options.mode == Options::Mode::Diagnostics) args.push_back(L"--diagnostics");
    else {
        // Run bootstrap privately while resolving user CLI paths from the original cwd.
        args.push_back(L"--cwd"); args.push_back(Absolute(std::filesystem::current_path()).native());
        args.push_back(L"--cli"); args.insert(args.end(), options.cli.begin(), options.cli.end());
    }
    auto captured = RunOwned(args, root, Environment(root), std::chrono::seconds(prepare ? 120 : 3700), prepare);
    if (!prepare) {
        bool delivered = Output(GetStdHandle(STD_OUTPUT_HANDLE), captured.output);
        Output(GetStdHandle(STD_ERROR_HANDLE), captured.error);
        if (!delivered) {
            auto text = captured.output.empty() ? captured.error : captured.output;
            if (text.size() > 3500) text.resize(3500);
            std::wstring message;
            try { message = Wide(text); } catch (...) { message = L"Diagnostic output is not valid UTF-8."; }
            MessageBoxW(nullptr, message.c_str(), L"Verilog to Digital", MB_OK | (captured.exit_code ? MB_ICONERROR : MB_ICONINFORMATION));
        }
        return static_cast<int>(captured.exit_code);
    }
    if (captured.exit_code) {
        auto detail = captured.error.empty() ? captured.output : captured.error;
        if (detail.size() > 3500) detail.resize(3500);
        throw std::runtime_error("Portable runtime validation failed: " + detail);
    }
    auto plan = ParsePlan(captured.output, root);
    auto gui = StartGui(plan, root, options);
    if (options.mode == Options::Mode::Smoke) {
        DWORD wait = WaitForSingleObject(gui.Get(), 30000);
        if (wait != WAIT_OBJECT_0) {
            TerminateProcess(gui.Get(), ERROR_TIMEOUT); WaitForSingleObject(gui.Get(), 5000);
            throw std::runtime_error("Desktop startup check did not complete within 30 seconds");
        }
    } else if (WaitForSingleObject(gui.Get(), 150) == WAIT_TIMEOUT) return 0;
    DWORD code = 0; Check(GetExitCodeProcess(gui.Get(), &code), "Read desktop startup status");
    if (code && code != STILL_ACTIVE) throw std::runtime_error("Desktop could not start (Windows exit " + std::to_string(code) + ")");
    return 0;
}
}  // namespace v2d::portable

#ifndef V2D_PORTABLE_LAUNCHER_TEST
int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    using namespace v2d::portable;
    SetErrorMode(GetErrorMode() | SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    bool headless = false;  // An MCP host cannot dismiss a message box; stderr and the exit code must suffice.
    try {
        int argc = 0; auto** raw = CommandLineToArgvW(GetCommandLineW(), &argc);
        if (!raw) throw std::runtime_error("Cannot read launcher arguments");
        std::vector<std::wstring> args(raw + 1, raw + argc); LocalFree(raw);
        headless = !args.empty() && args.front() == L"--mcp";
        return Run(Executable().parent_path(), ParseOptions(args));
    } catch (const std::exception& error) {
        std::string message = error.what();
        auto json = Json{{"schema_version",1},{"state","failed"},{"error",message}}.dump(-1, ' ', false, Json::error_handler_t::replace) + "\n";
        if (!Output(GetStdHandle(STD_ERROR_HANDLE), json) && !headless) {
            std::wstring text;
            try { text = Wide(message); } catch (...) { text = L"Portable startup failed. Check that the package is complete."; }
            if (text.size() > 3500) text.resize(3500);
            MessageBoxW(nullptr, text.c_str(), L"Verilog to Digital", MB_OK | MB_ICONERROR);
        }
        return 1;
    }
}
#endif
