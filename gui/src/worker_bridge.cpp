#include "worker_bridge.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cwctype>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <stdexcept>
#include <thread>

namespace v2d {
wxDEFINE_EVENT(EVT_V2D_WORKER, wxThreadEvent);
namespace {
using Clock = std::chrono::steady_clock;
using Json = nlohmann::json;

class Handle {
public:
    Handle() = default;
    explicit Handle(HANDLE value) : value_(value) {}
    ~Handle() { reset(); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    Handle(Handle&& other) noexcept : value_(other.release()) {}
    Handle& operator=(Handle&& other) noexcept { reset(other.release()); return *this; }
    HANDLE get() const { return value_; }
    explicit operator bool() const { return value_ && value_ != INVALID_HANDLE_VALUE; }
    HANDLE release() { HANDLE result = value_; value_ = nullptr; return result; }
    void reset(HANDLE value = nullptr) { if (*this) CloseHandle(value_); value_ = value; }
private:
    HANDLE value_ = nullptr;
};

struct Failure : std::runtime_error {
    std::string code;
    Failure(std::string code, const std::string& message) : std::runtime_error(message), code(std::move(code)) {}
};

void Check(BOOL ok, const char* operation) {
    if (!ok) throw Failure("BRIDGE_OS", std::string(operation) + " failed (Windows error " + std::to_string(GetLastError()) + ")");
}

std::int64_t Now() { return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count(); }

std::wstring Wide(const std::string& text) {
    if (text.empty()) return {};
    int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), static_cast<int>(text.size()), nullptr, 0);
    if (!size) throw Failure("BRIDGE_PROTOCOL", "Worker output is not valid UTF-8");
    std::wstring result(size, L'\0');
    Check(MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), static_cast<int>(text.size()), result.data(), size), "Decode UTF-8");
    return result;
}

void ValidWide(const std::wstring& value) {
    if (value.find(L'\0') != std::wstring::npos) throw Failure("BRIDGE_ARGUMENT", "Arguments cannot contain NUL characters");
    if (!value.empty() && !WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr))
        throw Failure("BRIDGE_ARGUMENT", "Arguments must contain valid Unicode scalar values");
}

std::wstring Quote(const std::wstring& argument) {
    ValidWide(argument);
    std::wstring result = L"\"";
    std::size_t slashes = 0;
    for (wchar_t c : argument) {
        if (c == L'\\') { ++slashes; continue; }
        if (c == L'"') result.append(slashes * 2 + 1, L'\\');
        else result.append(slashes, L'\\');
        slashes = 0;
        result += c;
    }
    result.append(slashes * 2, L'\\');
    result += L'"';
    return result;
}

std::wstring CommandLine(const std::vector<std::wstring>& args) {
    if (args.empty() || args.size() > 4096) throw Failure("BRIDGE_ARGUMENT", "An executable and a bounded argument list are required");
    std::wstring command;
    for (const auto& arg : args) {
        if (!command.empty()) command += L' ';
        command += Quote(arg);
        if (command.size() >= 32767) throw Failure("BRIDGE_ARGUMENT", "Windows command-line length exceeds 32766 characters");
    }
    return command;
}

std::wstring Lower(std::wstring value) {
    std::transform(value.begin(), value.end(), value.begin(), [](wchar_t c) { return static_cast<wchar_t>(std::towlower(c)); });
    return value;
}

std::filesystem::path Absolute(const std::filesystem::path& path) {
    auto result = std::filesystem::absolute(path).lexically_normal();
    ValidWide(result.native());
    return result;
}

// The CLI reports Path.resolve() results; compare junctions, substituted and
// mapped drives and short names in that final form.
std::filesystem::path Final(const std::filesystem::path& value) {
    const auto path = Absolute(value);
    Handle handle(CreateFileW(path.c_str(), 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                              OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr));
    if (!handle) return path;
    std::vector<wchar_t> buffer(32768);
    const auto count = GetFinalPathNameByHandleW(handle.get(), buffer.data(), static_cast<DWORD>(buffer.size()), FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (!count || count >= buffer.size()) return path;
    std::wstring text(buffer.data(), count);
    if (text.rfind(L"\\\\?\\UNC\\", 0) == 0) text = L"\\\\" + text.substr(8);
    else if (text.rfind(L"\\\\?\\", 0) == 0) text = text.substr(4);
    return Absolute(text);
}

std::vector<wchar_t> Environment(const std::filesystem::path& root, const std::filesystem::path& jobs) {
    std::map<std::wstring, std::wstring> values;
    LPWCH inherited = GetEnvironmentStringsW();
    if (!inherited) throw Failure("BRIDGE_OS", "Cannot read the process environment");
    for (const wchar_t* at = inherited; *at; at += std::wcslen(at) + 1) {
        std::wstring item(at);
        const auto split = item.find(L'=', item.front() == L'=' ? 1 : 0);
        if (split != std::wstring::npos) values[Lower(item.substr(0, split))] = item.substr(split + 1);
    }
    FreeEnvironmentStringsW(inherited);
    for (const auto* name : {L"pythonhome", L"pythonpath", L"pythonuserbase", L"pythonstartup", L"pythoninspect"}) values.erase(name);
    values[L"pythonutf8"] = L"1";
    values[L"pythonnousersite"] = L"1";
    values[L"virtual_env"] = (root / L".venv").native();
    values[L"v2d_root"] = root.native();
    values[L"v2d_job_root"] = jobs.native();
    std::vector<wchar_t> result;
    for (const auto& [name, value] : values) {
        std::wstring item = name + L'=' + value;
        result.insert(result.end(), item.begin(), item.end());
        result.push_back(L'\0');
    }
    result.push_back(L'\0');
    return result;
}

struct Pipe {
    Handle read, write;
    void Open() {
        SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
        HANDLE reader = nullptr, writer = nullptr;
        Check(CreatePipe(&reader, &writer, &security, 0), "Create output pipe");
        read.reset(reader); write.reset(writer);
        Check(SetHandleInformation(read.get(), HANDLE_FLAG_INHERIT, 0), "Protect parent pipe");
    }
};

struct Child {
    Handle process;
    Handle output, error;
    bool output_eof = false, error_eof = false;
    DWORD pid = 0;
    std::optional<DWORD> Exit() const {
        DWORD wait = WaitForSingleObject(process.get(), 0);
        if (wait == WAIT_TIMEOUT) return {};
        Check(wait == WAIT_OBJECT_0, "Read worker completion");
        DWORD code = 0;
        Check(GetExitCodeProcess(process.get(), &code), "Read worker exit code");
        return code;
    }
};

Child Spawn(const std::vector<std::wstring>& args, const std::filesystem::path& cwd,
            std::vector<wchar_t>& environment, HANDLE job, const std::atomic<bool>* stop = nullptr) {
    Pipe out, err;
    out.Open(); err.Open();
    SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
    Handle input(CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &security,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr));
    Check(static_cast<bool>(input), "Open null input");
    SIZE_T size = 0;
    InitializeProcThreadAttributeList(nullptr, 2, 0, &size);
    std::vector<unsigned char> storage(size);
    auto* attributes = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(storage.data());
    Check(InitializeProcThreadAttributeList(attributes, 2, 0, &size), "Initialize startup attributes");
    struct DeleteAttributes { LPPROC_THREAD_ATTRIBUTE_LIST value; ~DeleteAttributes() { DeleteProcThreadAttributeList(value); } } cleanup{attributes};
    HANDLE handles[]{input.get(), out.write.get(), err.write.get()};
    HANDLE jobs[]{job};
    // Atomic job association closes the GUI-death gap before suspended setup.
    Check(UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, sizeof(jobs), nullptr, nullptr), "Assign ownership at creation");
    Check(UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, sizeof(handles), nullptr, nullptr), "Restrict inherited handles");
    STARTUPINFOEXW startup{};
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    startup.StartupInfo.wShowWindow = SW_HIDE;
    startup.StartupInfo.hStdInput = input.get();
    startup.StartupInfo.hStdOutput = out.write.get();
    startup.StartupInfo.hStdError = err.write.get();
    startup.lpAttributeList = attributes;
    PROCESS_INFORMATION info{};
    auto command = CommandLine(args);
    Check(CreateProcessW(args.front().c_str(), command.data(), nullptr, nullptr, TRUE,
        CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED,
        environment.data(), cwd.c_str(), &startup.StartupInfo, &info), "Start isolated CLI");
    Child child;
    child.process.reset(info.hProcess); child.pid = info.dwProcessId;
    Handle thread(info.hThread);
    try {
        BOOL member = FALSE;
        Check(IsProcessInJob(child.process.get(), job, &member), "Verify CLI ownership");
        if (!member) throw Failure("BRIDGE_ISOLATION", "CLI was not assigned to its owned Job");
        if (stop && stop->load()) {
            TerminateProcess(child.process.get(), ERROR_CANCELLED);
        } else if (ResumeThread(thread.get()) == static_cast<DWORD>(-1)) {
            throw Failure("BRIDGE_OS", "Cannot resume the owned CLI");
        }
    } catch (...) {
        TerminateProcess(child.process.get(), ERROR_CANCELLED);
        throw;
    }
    child.output = std::move(out.read); child.error = std::move(err.read);
    return child;
}

Json Parse(const std::string& line) {
    if (line.empty() || line.compare(0, 3, "\xEF\xBB\xBF") == 0) throw Failure("BRIDGE_PROTOCOL", "JSONL requires one JSON object without a byte-order mark");
    Wide(line);  // Validate complete UTF-8, including text outside JSON strings.
    std::vector<std::set<std::string>> objects;
    std::size_t nodes = 0;
    auto callback = [&](int depth, Json::parse_event_t event, Json& value) {
        if (depth > 64 || ++nodes > 262144) throw Failure("BRIDGE_PROTOCOL", "JSON exceeds its depth or node limit");
        if (event == Json::parse_event_t::object_start) objects.emplace_back();
        else if (event == Json::parse_event_t::object_end) objects.pop_back();
        else if (event == Json::parse_event_t::key && !objects.back().insert(value.get<std::string>()).second)
            throw Failure("BRIDGE_PROTOCOL", "JSON contains a duplicate object key");
        return true;
    };
    try {
        Json value = Json::parse(line, callback);
        if (!value.is_object() || !value.contains("schema_version") || !value["schema_version"].is_number_integer()
            || value["schema_version"] != 1 || !value.contains("type") || !value["type"].is_string())
            throw Failure("BRIDGE_PROTOCOL", "Invalid CLI event envelope");
        const auto type = value["type"].get<std::string>();
        const auto payload = type == "progress" ? "event" : "result";
        if ((type != "progress" && type != "result" && type != "error") || value.size() != 3
            || !value.contains(payload) || !value[payload].is_object())
            throw Failure("BRIDGE_PROTOCOL", "Unexpected CLI event fields or type");
        return value;
    } catch (const Json::exception& error) {
        throw Failure("BRIDGE_PROTOCOL", std::string("Invalid JSON from CLI: ") + error.what());
    }
}

// One reader per handle drains both streams on the controller thread.
// Read only the bytes PeekNamedPipe reports to avoid blocking on future writes.
template<class Consumer>
void Drain(HANDLE pipe, bool& eof, Consumer consume) {
    if (eof) return;
    for (unsigned batch = 0; batch < 16; ++batch) {
        DWORD available = 0;
        if (!PeekNamedPipe(pipe, nullptr, 0, nullptr, &available, nullptr)) {
            if (GetLastError() == ERROR_BROKEN_PIPE) { eof = true; return; }
            Check(FALSE, "Inspect worker output pipe");
        }
        if (!available) return;
        char buffer[8192];
        DWORD count = 0;
        Check(ReadFile(pipe, buffer, std::min<DWORD>(available, sizeof(buffer)), &count, nullptr), "Read available worker output");
        if (!count) { eof = true; return; }
        consume(buffer, count);
    }
}

std::string SafeTail(const std::string& text) {
    // Escape invalid log bytes so UI JSON remains valid UTF-8.
    std::string result;
    for (std::size_t at = 0; at < text.size();) {
        unsigned char first = static_cast<unsigned char>(text[at]);
        std::size_t length = first < 0x80 ? 1 : first >= 0xC2 && first <= 0xDF ? 2 : first >= 0xE0 && first <= 0xEF ? 3 : first >= 0xF0 && first <= 0xF4 ? 4 : 0;
        if (length && at + length <= text.size()) {
            auto piece = text.substr(at, length);
            if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, piece.data(), static_cast<int>(piece.size()), nullptr, 0)) {
                result += piece; at += length; continue;
            }
        }
        result += "\xEF\xBF\xBD"; ++at;
    }
    return result;
}
}  // namespace

struct WorkerBridge::Impl {
    struct Run {
        std::uint64_t generation = 0;
        BridgeOptions options;
        std::filesystem::path root, jobs;
        std::vector<wchar_t> environment;
        std::mutex native_mutex;
        Handle job;
        Child main;
        std::atomic<bool> cancel{false}, stop{false}, done{false};
        std::atomic<std::int64_t> force_at{0};
        std::atomic<std::size_t> coalesced{0};
        std::mutex done_mutex;
        std::condition_variable finished;
        std::int64_t deadline = 0;
        bool managed_command = false;
        bool desktop_command = false;
        void Force() {
            stop = true;
            std::lock_guard<std::mutex> lock(native_mutex);
            if (job) TerminateJobObject(job.get(), ERROR_CANCELLED);
        }
        DWORD Active() {
            std::lock_guard<std::mutex> lock(native_mutex);
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info{};
            Check(QueryInformationJobObject(job.get(), JobObjectBasicAccountingInformation, &info, sizeof(info), nullptr), "Read owned process accounting");
            return info.ActiveProcesses;
        }
    };
    struct Shared {
        std::mutex mutex;
        wxEvtHandler* target = nullptr;
        bool wake_pending = false, closing = false;
        std::optional<WorkerUpdate> progress, completion;
        std::shared_ptr<Run> run;
        void Publish(WorkerUpdate update, const std::shared_ptr<Run>& owner) {
            std::lock_guard<std::mutex> lock(mutex);
            if (update.kind == WorkerUpdate::Kind::Completed) {
                // Running() never reports completion before its update is stored, and a
                // Shutdown waiter holding done_mutex cannot miss the following notification.
                std::lock_guard<std::mutex> finished(owner->done_mutex);
                owner->done = true;
            }
            if (closing || run != owner) return;
            if (update.kind == WorkerUpdate::Kind::Progress) {
                if (progress) ++owner->coalesced;
                progress = std::move(update);
            } else completion = std::move(update);
            if (!wake_pending && target) {
                wake_pending = true;
                wxQueueEvent(target, new wxThreadEvent(EVT_V2D_WORKER));
            }
        }
    };
    std::shared_ptr<Shared> state = std::make_shared<Shared>();
    std::filesystem::path root, jobs;
    BridgeOptions options;
    std::thread controller;
    std::uint64_t generation = 0;

    static std::vector<std::wstring> Args(const std::filesystem::path& root, const std::vector<std::wstring>& args) {
        std::vector<std::wstring> result{(root / L".venv/Scripts/python.exe").native(), L"-I", L"-X", L"utf8", L"-u", (root / L"scripts/v2d.py").native()};
        result.insert(result.end(), args.begin(), args.end());
        if (std::find(args.begin(), args.end(), L"--events") == args.end()) result.push_back(L"--events");
        return result;
    }

    static void Control(std::shared_ptr<Shared> state, std::shared_ptr<Run> run) {
        WorkerUpdate final;
        final.kind = WorkerUpdate::Kind::Completed; final.generation = run->generation;
        std::string line, tail, job_id;
        std::optional<std::filesystem::path> manifest;
        std::optional<Json> terminal;
        std::optional<Child> cancellation;
        std::size_t cancel_bytes = 0;
        std::optional<std::int64_t> exited_at;
        auto bind_job = [&](const Json& payload, bool allow_manifest_only = false) {
            if (!payload.contains("job_id") && !payload.contains("job_manifest")) return;
            if (!payload.contains("job_manifest") || !payload["job_manifest"].is_string()
                || (payload.contains("job_id") && !payload["job_id"].is_string())
                || (!payload.contains("job_id") && !allow_manifest_only))
                throw Failure("BRIDGE_PROTOCOL", "Incomplete job identity in CLI event");
            auto supplied_manifest = Absolute(Wide(payload["job_manifest"].get<std::string>()));
            auto id = payload.contains("job_id") ? payload["job_id"].get<std::string>()
                : supplied_manifest.parent_path().filename().u8string();
            if (id.size() != 32 || id.find_first_not_of("0123456789abcdef") != std::string::npos || (!job_id.empty() && job_id != id))
                throw Failure("BRIDGE_PROTOCOL", "CLI job identity changed or is invalid");
            auto expected = Absolute(run->jobs / Wide(id) / L"job.json");
            if (Lower(supplied_manifest.native()) != Lower(expected.native())
                && Lower(Final(supplied_manifest).native()) != Lower(Final(expected).native()))
                throw Failure("BRIDGE_PROTOCOL", "CLI job manifest is outside its bound job directory");
            job_id = id; manifest = expected;
        };
        auto output = [&](const char* data, std::size_t count) {
            final.stdout_bytes += count;
            if (final.stdout_bytes > run->options.max_stdout_bytes) throw Failure("BRIDGE_OUTPUT_LIMIT", "CLI stdout exceeded its byte limit");
            for (std::size_t i = 0; i < count; ++i) {
                if (data[i] != '\n') {
                    line += data[i];
                    if (line.size() > run->options.max_line_bytes) throw Failure("BRIDGE_OUTPUT_LIMIT", "CLI JSONL line exceeded its byte limit");
                    continue;
                }
                if (terminal) throw Failure("BRIDGE_PROTOCOL", "CLI emitted output after its final envelope");
                Json envelope = Parse(line); line.clear();
                if (envelope["type"] == "progress") {
                    const auto& event = envelope["event"];
                    if (!event.contains("stage") || !event["stage"].is_string() || event["stage"].get_ref<const std::string&>().empty()
                        || event["stage"].get_ref<const std::string&>().size() > 128)
                        throw Failure("BRIDGE_PROTOCOL", "CLI progress requires a bounded stage");
                    if (run->managed_command) bind_job(event);
                    WorkerUpdate update; update.generation = run->generation; update.envelope = std::move(envelope);
                    state->Publish(std::move(update), run);
                } else {
                    if (run->managed_command) bind_job(envelope["result"], run->desktop_command);
                    terminal = std::move(envelope);
                }
            }
        };
        auto error_output = [&](const char* data, std::size_t count) {
            final.stderr_bytes += count;
            tail.append(data, count);
            if (tail.size() > run->options.stderr_tail_bytes) tail.erase(0, tail.size() - run->options.stderr_tail_bytes);
            if (final.stderr_bytes > run->options.max_stderr_bytes) throw Failure("BRIDGE_OUTPUT_LIMIT", "CLI stderr exceeded its byte limit");
        };
        try {
            for (;;) {
                Drain(run->main.output.get(), run->main.output_eof, output);
                Drain(run->main.error.get(), run->main.error_eof, error_output);
                auto exit = run->main.Exit();
                if (exit && !exited_at) {
                    exited_at = Now(); final.exit_code = *exit;
                    if (cancellation && !cancellation->Exit()) TerminateProcess(cancellation->process.get(), ERROR_CANCELLED);
                }
                if (cancellation) {
                    auto sink = [&](const char*, std::size_t size) { cancel_bytes += size; if (cancel_bytes > 1024 * 1024) throw Failure("BRIDGE_OUTPUT_LIMIT", "Cancellation helper exceeded its output limit"); };
                    Drain(cancellation->output.get(), cancellation->output_eof, sink);
                    Drain(cancellation->error.get(), cancellation->error_eof, sink);
                }
                if (exit && run->main.output_eof && run->main.error_eof && run->Active() == 0) break;
                if (run->cancel) {
                    std::int64_t unset = 0;
                    run->force_at.compare_exchange_strong(unset, Now() + run->options.cancellation_grace.count());
                    if (!exit && manifest && !cancellation && !run->stop && Now() < run->force_at)
                        cancellation = Spawn(Args(run->root, {L"job", L"cancel", manifest->native()}), run->root, run->environment, run->job.get(), &run->stop);
                    if (!exit && Now() >= run->force_at) throw Failure("CANCELLED", "Cancellation required termination of the owned CLI tree");
                }
                if (run->stop) throw Failure("CANCELLED", "The owned CLI was stopped during shutdown");
                if (Now() >= run->deadline) throw Failure("BRIDGE_TIMEOUT", "CLI exceeded its complete command deadline");
                if (exited_at && Now() - *exited_at > 1500) throw Failure("BRIDGE_PROCESS_LEAK", "CLI exited while an owned descendant or output pipe remained active");
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
            if (!line.empty()) throw Failure("BRIDGE_PROTOCOL", "CLI ended with an unterminated JSONL line");
            if (!terminal) throw Failure("BRIDGE_PROTOCOL", "CLI exited without a final envelope");
            const auto& result = (*terminal)["result"];
            const bool failed = (result.contains("state") && result["state"].is_string()
                && std::set<std::string>{"failed", "incomplete", "cancelled", "interrupted"}.count(result["state"].get<std::string>()))
                || (result.contains("valid") && result["valid"] == false) || (*terminal)["type"] == "error";
            if (final.exit_code != (failed ? 1u : 0u))
                throw Failure("BRIDGE_EXIT", "CLI exit code disagrees with its final envelope");
            final.envelope = std::move(*terminal);
        } catch (const Failure& error) {
            final.diagnostic_code = error.code; final.diagnostic_message = error.what(); final.forced = true;
            run->Force();
        } catch (const std::exception& error) {
            final.diagnostic_code = "BRIDGE_FAILURE"; final.diagnostic_message = error.what(); final.forced = true;
            run->Force();
        }
        try {
            const auto until = Now() + 1500;
            while ((final.remaining_processes = run->Active()) != 0 && Now() < until) std::this_thread::sleep_for(std::chrono::milliseconds(10));
            auto exit = run->main.Exit();
            if (exit) final.exit_code = *exit;
            if (final.remaining_processes) {
                final.envelope = nullptr; final.diagnostic_code = "BRIDGE_PROCESS_LEAK";
                final.diagnostic_message = "Owned processes did not finish terminating";
            }
        } catch (const std::exception& error) {
            final.envelope = nullptr; final.diagnostic_code = "BRIDGE_CLEANUP"; final.diagnostic_message = error.what();
        }
        {
            std::lock_guard<std::mutex> lock(run->native_mutex);
            run->job.reset();  // Last owned Job handle; never inherited.
        }
        run->main = Child{};
        cancellation.reset();
        final.stderr_tail = SafeTail(tail);
        if (final.stderr_tail.size() > run->options.stderr_tail_bytes) {
            final.stderr_tail.erase(0, final.stderr_tail.size() - run->options.stderr_tail_bytes);
            while (!final.stderr_tail.empty() && (static_cast<unsigned char>(final.stderr_tail.front()) & 0xC0) == 0x80)
                final.stderr_tail.erase(0, 1);
        }
        final.cancellation_requested = run->cancel;
        final.coalesced_progress = run->coalesced;
        state->Publish(std::move(final), run);  // Also marks the run done.
        run->finished.notify_all();
    }
};

WorkerBridge::WorkerBridge(wxEvtHandler& target, std::filesystem::path root, BridgeOptions options) : impl_(std::make_unique<Impl>()) {
    impl_->root = Absolute(root); impl_->options = std::move(options);
    const auto& opt = impl_->options;
    if (opt.command_timeout.count() <= 0 || opt.command_timeout > std::chrono::hours(24)
        || opt.cancellation_grace.count() < 0 || opt.cancellation_grace > std::chrono::seconds(30)
        || !opt.max_line_bytes || opt.max_line_bytes > 8 * 1024 * 1024 || opt.max_stdout_bytes < opt.max_line_bytes
        || opt.max_stdout_bytes > 16 * 1024 * 1024 || !opt.max_stderr_bytes || opt.max_stderr_bytes > 1024 * 1024
        || !opt.stderr_tail_bytes || opt.stderr_tail_bytes > 64 * 1024)
        throw Failure("BRIDGE_ARGUMENT", "Invalid worker bridge resource limits");
    auto jobs = opt.job_root;
    if (jobs.empty()) {
        const DWORD size = GetEnvironmentVariableW(L"V2D_JOB_ROOT", nullptr, 0);
        if (size) {
            std::vector<wchar_t> inherited(size);
            const DWORD copied = GetEnvironmentVariableW(L"V2D_JOB_ROOT", inherited.data(), size);
            if (!copied || copied >= size) throw Failure("BRIDGE_ARGUMENT", "Cannot read V2D_JOB_ROOT consistently");
            jobs = std::wstring(inherited.data(), copied);
        }
    }
    impl_->jobs = Absolute(jobs.empty() ? impl_->root / L".runs/jobs" : jobs);
    if (std::any_of(impl_->jobs.native().begin(), impl_->jobs.native().end(), [](wchar_t c) { return c > 127; }))
        throw Failure("BRIDGE_ARGUMENT", "Native job storage requires an ASCII path; set V2D_JOB_ROOT to an ASCII directory");
    impl_->state->target = &target;
    SetErrorMode(GetErrorMode() | SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
}

WorkerBridge::~WorkerBridge() { Shutdown(std::chrono::milliseconds(0)); }

std::uint64_t WorkerBridge::Start(const std::vector<std::wstring>& cliArgs) {
    if (cliArgs.empty()) throw Failure("BRIDGE_ARGUMENT", "A v2d command is required");
    std::unique_lock<std::mutex> lock(impl_->state->mutex);
    if (impl_->state->closing) throw Failure("BRIDGE_CLOSED", "The worker bridge has shut down");
    if (impl_->state->run && !impl_->state->run->done) throw Failure("BRIDGE_BUSY", "A CLI command is already running");
    lock.unlock();
    if (impl_->controller.joinable()) impl_->controller.join();
    auto run = std::make_shared<Impl::Run>();
    run->generation = ++impl_->generation; run->options = impl_->options; run->root = impl_->root; run->jobs = impl_->jobs;
    run->managed_command = cliArgs.front() == L"convert" || cliArgs.front() == L"inspect" || cliArgs.front() == L"validate";
    run->desktop_command = cliArgs.size() >= 2 && cliArgs.front() == L"desktop"
        && (cliArgs[1] == L"result" || cliArgs[1] == L"export" || cliArgs[1] == L"digital");
    run->managed_command = run->managed_command || run->desktop_command;
    run->environment = Environment(run->root, run->jobs);
    run->deadline = Now() + run->options.command_timeout.count();
    run->job.reset(CreateJobObjectW(nullptr, nullptr));
    Check(static_cast<bool>(run->job), "Create owned CLI Job");
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
    limits.BasicLimitInformation.ActiveProcessLimit = 128;
    Check(SetInformationJobObject(run->job.get(), JobObjectExtendedLimitInformation, &limits, sizeof(limits)), "Set CLI ownership limits");
    run->main = Spawn(Impl::Args(run->root, cliArgs), run->root, run->environment, run->job.get());
    lock.lock();
    impl_->state->run = run; impl_->state->progress.reset(); impl_->state->completion.reset();
    lock.unlock();
    try { impl_->controller = std::thread(Impl::Control, impl_->state, run); }
    catch (...) { run->Force(); run->done = true; throw; }
    return run->generation;
}

void WorkerBridge::Cancel() {
    std::lock_guard<std::mutex> lock(impl_->state->mutex);
    if (impl_->state->run && !impl_->state->run->done) impl_->state->run->cancel = true;
}

void WorkerBridge::Shutdown(std::chrono::milliseconds grace) {
    std::shared_ptr<Impl::Run> run;
    {
        std::lock_guard<std::mutex> lock(impl_->state->mutex);
        impl_->state->closing = true; impl_->state->target = nullptr;
        impl_->state->progress.reset(); impl_->state->completion.reset();
        run = impl_->state->run;
    }
    if (run && !run->done) {
        grace = std::clamp(grace, std::chrono::milliseconds(0), std::chrono::milliseconds(30000));
        run->cancel = true;
        auto proposed = Now() + grace.count();
        auto current = run->force_at.load();
        while ((current == 0 || current > proposed) && !run->force_at.compare_exchange_weak(current, proposed)) {}
        std::unique_lock<std::mutex> lock(run->done_mutex);
        if (!run->finished.wait_for(lock, grace + std::chrono::milliseconds(1800), [&] { return run->done.load(); })) run->Force();
    }
    if (impl_->controller.joinable()) {
        if (!run || run->done) impl_->controller.join();
        else impl_->controller.detach();  // Shared state owns cleanup; event target is detached.
    }
}

bool WorkerBridge::Running() const {
    std::lock_guard<std::mutex> lock(impl_->state->mutex);
    return impl_->state->run && !impl_->state->run->done;
}

std::vector<WorkerUpdate> WorkerBridge::DrainEvents() {
    std::lock_guard<std::mutex> lock(impl_->state->mutex);
    std::vector<WorkerUpdate> updates;
    if (impl_->state->progress) updates.push_back(std::move(*impl_->state->progress));
    if (impl_->state->completion) updates.push_back(std::move(*impl_->state->completion));
    impl_->state->progress.reset(); impl_->state->completion.reset(); impl_->state->wake_pending = false;
    return updates;
}

std::uint32_t LaunchDetached(const std::vector<std::wstring>& args, const std::filesystem::path& cwd) {
    auto command = CommandLine(args);
    auto executable = std::filesystem::path(args.front());
    if (!executable.is_absolute() || Lower(executable.extension().native()) != L".exe")
        throw Failure("BRIDGE_ARGUMENT", "Open requires a checked absolute executable path");
    auto directory = Absolute(cwd);
    STARTUPINFOW startup{}; startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESHOWWINDOW; startup.wShowWindow = SW_SHOWNORMAL;
    PROCESS_INFORMATION process{};
    Check(CreateProcessW(executable.c_str(), command.data(), nullptr, nullptr, FALSE,
        CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, nullptr, directory.c_str(), &startup, &process), "Open checked application");
    Handle owned_process(process.hProcess), owned_thread(process.hThread);
    return process.dwProcessId;
}
}  // namespace v2d
