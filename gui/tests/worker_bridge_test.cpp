#include "worker_bridge.h"
#include <wx/init.h>
#include <wx/app.h>
#include <windows.h>
#include <shellapi.h>
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <thread>

namespace fs = std::filesystem;
using namespace std::chrono_literals;
using v2d::WorkerBridge;
using v2d::WorkerUpdate;
using Json = nlohmann::json;

void Require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
std::string Utf8(const std::wstring& value) { return wxString(value).utf8_string(); }

struct Process {
    HANDLE handle = nullptr;
    explicit Process(DWORD pid) : handle(OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, FALSE, pid)) { Require(handle != nullptr, "Cannot open our test child"); }
    ~Process() { if (handle) { if (WaitForSingleObject(handle, 0) == WAIT_TIMEOUT) TerminateProcess(handle, 1); WaitForSingleObject(handle, 3000); CloseHandle(handle); } }
    void Kill() { Require(TerminateProcess(handle, 1), "Cannot stop our test parent"); }
    bool Done(DWORD timeout = 0) const { return WaitForSingleObject(handle, timeout) == WAIT_OBJECT_0; }
};

struct Target : wxEvtHandler {
    unsigned wakes = 0;
    Target() { Bind(v2d::EVT_V2D_WORKER, [this](wxThreadEvent&) { ++wakes; }); }
};

void Pump() { if (wxTheApp) wxTheApp->ProcessPendingEvents(); std::this_thread::sleep_for(5ms); }
void Wait(WorkerBridge& bridge) {
    const auto deadline = std::chrono::steady_clock::now() + 12s;
    while (bridge.Running() && std::chrono::steady_clock::now() < deadline) Pump();
    Require(!bridge.Running(), "Bridge did not finish within test bound");
    Pump();
}
WorkerUpdate Completion(WorkerBridge& bridge) {
    Wait(bridge);
    auto events = bridge.DrainEvents();
    for (auto& event : events) if (event.kind == WorkerUpdate::Kind::Completed) return std::move(event);
    throw std::runtime_error("Missing final bridge update");
}
Json ReadMarker(const fs::path& path) {
    const auto deadline = std::chrono::steady_clock::now() + 5s;
    while (!fs::exists(path) && std::chrono::steady_clock::now() < deadline) Pump();
    Require(fs::exists(path), "Fixture marker did not arrive");
    std::ifstream stream(path); Json value; stream >> value; return value;
}

int main() {
    int argc = 0;
    auto** raw = CommandLineToArgvW(GetCommandLineW(), &argc);
    std::vector<std::wstring> args(raw, raw + argc); LocalFree(raw);
    try {
        Require(argc >= 2, "Provide repository root");
        if (args[1] == L"--sleep") { Sleep(60000); return 0; }
        wxInitializer initialized;
        Require(initialized.IsOk(), "wxWidgets initialization failed");
        Target target;
        if (args[1] == L"--owning-parent") {
            Require(argc == 5, "Parent fixture arguments");
            v2d::BridgeOptions options; options.job_root = args[3];
            WorkerBridge bridge(target, args[2], options);
            bridge.Start({L"doctor", L"--fixture", L"hang-child", L"--marker", args[4]});
            for (;;) Pump();
        }
        const fs::path repository = fs::absolute(args[1]);
        const auto base = repository / L".runs/gui-bridge-native" / (std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64()));
        const auto root = base / L"hdl_\u0131\u015f\u0131k_\u03c0";
        fs::create_directories(root / L".venv/Scripts"); fs::create_directories(root / L"scripts");
        fs::copy_file(repository / L".venv/Scripts/python.exe", root / L".venv/Scripts/python.exe");
        fs::copy_file(repository / L".venv/pyvenv.cfg", root / L".venv/pyvenv.cfg");
        fs::copy_file(repository / L"gui/tests/bridge_fixture.py", root / L"scripts/v2d.py");
        v2d::BridgeOptions options; options.job_root = base / L"jobs";
        unsigned checks = 0;
        {
            SetEnvironmentVariableW(L"PYTHONHOME", L"C:\\must-not-be-used");
            SetEnvironmentVariableW(L"PYTHONPATH", L"C:\\must-not-be-imported");
            WorkerBridge bridge(target, root, options);
            std::vector<std::wstring> cli{L"doctor", L"--fixture", L"echo", L"", L"path with spaces\\", L"a\"b", L"\\\\server\\share\\", L"\u0131\u015f\u0131k_\u03c0", L"& echo should-never-run"};
            auto generation = bridge.Start(cli);
            SetEnvironmentVariableW(L"PYTHONHOME", nullptr); SetEnvironmentVariableW(L"PYTHONPATH", nullptr);
            auto result = Completion(bridge);
            Require(result.generation == generation && result.diagnostic_code.empty() && result.exit_code == 0, "Unicode CLI failed");
            auto expected = Json::array(); for (const auto& item : cli) expected.push_back(Utf8(item)); expected.push_back("--events");
            Require(result.envelope["result"]["argv"] == expected, "Arguments were not preserved exactly");
            Require(result.envelope["result"]["isolated"] == 1 && result.envelope["result"]["pythonhome"].is_null()
                && result.envelope["result"]["pythonpath"].is_null(), "Interpreter isolation was lost");
            Require(result.remaining_processes == 0 && result.stderr_tail.find("diagnostic") != std::string::npos, "Completion lost logs or ownership");
            Require(result.envelope["result"]["root"] == Utf8(root.lexically_normal().native())
                && result.envelope["result"]["job_root"] == Utf8(options.job_root.lexically_normal().native()), "Explicit worker root/job binding was lost");
            ++checks;
        }
        {
            SetEnvironmentVariableW(L"V2D_ROOT", L"C:\\wrong-application");
            SetEnvironmentVariableW(L"V2D_JOB_ROOT", (base / L"inherited-jobs").c_str());
            WorkerBridge bridge(target, root);
            bridge.Start({L"doctor", L"--fixture", L"echo"});
            auto result = Completion(bridge);
            SetEnvironmentVariableW(L"V2D_ROOT", nullptr); SetEnvironmentVariableW(L"V2D_JOB_ROOT", nullptr);
            Require(result.diagnostic_code.empty() && result.envelope["result"]["root"] == Utf8(root.lexically_normal().native())
                && result.envelope["result"]["job_root"] == Utf8((base / L"inherited-jobs").lexically_normal().native()),
                "Inherited ASCII job root or explicit application root was ignored");
            ++checks;
        }
        {
            auto bad = options; bad.job_root = root / L"jobs";
            bool rejected = false;
            try { WorkerBridge bridge(target, root, bad); } catch (const std::exception& error) {
                rejected = std::string(error.what()).find("ASCII") != std::string::npos;
            }
            Require(rejected, "Non-ASCII native job storage was accepted"); ++checks;
        }
        for (const auto* mode : {L"duplicate", L"invalid-utf8", L"surrogate", L"nonfinite", L"depth", L"extra-field", L"unterminated", L"after-result", L"empty"}) {
            WorkerBridge bridge(target, root, options);
            bridge.Start({L"doctor", L"--fixture", mode});
            auto result = Completion(bridge);
            Require(result.diagnostic_code == "BRIDGE_PROTOCOL" && result.envelope.is_null() && result.remaining_processes == 0, "Malformed protocol was accepted");
            ++checks;
        }
        for (const auto* mode : {L"oversize", L"stderr-flood"}) {
            auto limits = options; limits.max_line_bytes = 1024; limits.max_stdout_bytes = 2048; limits.max_stderr_bytes = 1024;
            WorkerBridge bridge(target, root, limits);
            bridge.Start({L"doctor", L"--fixture", mode});
            auto result = Completion(bridge);
            Require(result.diagnostic_code == "BRIDGE_OUTPUT_LIMIT" && result.forced && result.remaining_processes == 0, "Output limit did not terminate owned process");
            ++checks;
        }
        {
            WorkerBridge bridge(target, root, options);
            bridge.Start({L"doctor", L"--fixture", L"exit-mismatch"});
            auto result = Completion(bridge);
            Require(result.diagnostic_code == "BRIDGE_EXIT" && result.envelope.is_null(), "Mismatched exit became a result"); ++checks;
        }
        {
            auto limits = options; limits.stderr_tail_bytes = 64;
            WorkerBridge bridge(target, root, limits);
            bridge.Start({L"doctor", L"--fixture", L"log-bytes"});
            auto result = Completion(bridge);
            Require(result.diagnostic_code.empty() && result.stderr_tail.size() <= 64, "Invalid log bytes exceeded retained bound");
            (void)Json(result.stderr_tail).dump(); ++checks;
        }
        {
            Target slow;
            WorkerBridge bridge(slow, root, options);
            bridge.Start({L"doctor", L"--fixture", L"flood"});
            auto result = Completion(bridge);
            Require(slow.wakes <= 1 && result.coalesced_progress > 1000 && result.diagnostic_code.empty(), "Progress flooded the event queue"); ++checks;
        }
        for (int attempt = 0; attempt < 8; ++attempt) {
            // No pump or sleep: completion and its stored update become visible together.
            WorkerBridge bridge(target, root, options);
            bridge.Start({L"doctor", L"--fixture", L"echo"});
            const auto deadline = std::chrono::steady_clock::now() + 12s;
            while (bridge.Running() && std::chrono::steady_clock::now() < deadline) std::this_thread::yield();
            auto events = bridge.DrainEvents();
            Require(!bridge.Running() && std::any_of(events.begin(), events.end(), [](const WorkerUpdate& item) {
                return item.kind == WorkerUpdate::Kind::Completed && item.diagnostic_code.empty(); }), "Completion was visible before its update");
            if (attempt == 7) ++checks;
        }
        {
            WorkerBridge bridge(target, root, options);
            auto old = bridge.Start({L"doctor", L"--fixture", L"echo"}); Wait(bridge);
            auto current = bridge.Start({L"doctor", L"--fixture", L"echo"});
            auto result = Completion(bridge);
            Require(current > old && result.generation == current, "Stale generation reached current result"); ++checks;
        }
        {
            WorkerBridge bridge(target, root, options);
            bridge.Start({L"convert", L"--fixture", L"late-job"}); bridge.Cancel();
            auto result = Completion(bridge);
            Require(result.cancellation_requested && !result.forced && result.envelope["result"]["state"] == "cancelled", "Pre-ID cancellation was not latched through job cancel"); ++checks;
        }
        {
            WorkerBridge bridge(target, root, options);
            bridge.Start({L"convert", L"--fixture", L"wrong-job-path"});
            Require(Completion(bridge).diagnostic_code == "BRIDGE_PROTOCOL", "Foreign cancel manifest was accepted"); ++checks;
        }
        {
            WorkerBridge bridge(target, root, options);
            bridge.Start({L"desktop", L"result", L"--fixture", L"late-job"}); bridge.Cancel();
            auto result = Completion(bridge);
            Require(result.cancellation_requested && !result.forced && result.diagnostic_code.empty()
                && result.envelope["result"]["state"] == "cancelled", "Desktop cancellation did not use its owned validation job"); ++checks;
        }
        {
            WorkerBridge bridge(target, root, options);
            bridge.Start({L"desktop", L"result", L"--fixture", L"desktop-final"});
            auto result = Completion(bridge);
            Require(result.diagnostic_code.empty() && result.exit_code == 0 && result.remaining_processes == 0,
                "Desktop terminal manifest without redundant job ID was rejected"); ++checks;
        }
        {
            // A job root reached through a junction (or substituted/mapped drive) is reported resolved.
            const auto resolved = base / L"resolved-jobs", alias = base / L"jobs-alias";
            fs::create_directories(resolved);
            wchar_t system[MAX_PATH]; Require(GetSystemDirectoryW(system, MAX_PATH) != 0, "Cannot locate cmd.exe");
            std::wstring command = L"\"" + (fs::path(system) / L"cmd.exe").native() + L"\" /d /s /c \"mklink /J \"" + alias.native() + L"\" \"" + resolved.native() + L"\"\"";
            STARTUPINFOW startup{}; startup.cb = sizeof(startup); PROCESS_INFORMATION info{};
            Require(CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, nullptr, &startup, &info) != 0, "Cannot start junction fixture");
            WaitForSingleObject(info.hProcess, 10000); CloseHandle(info.hThread); CloseHandle(info.hProcess);
            Require(fs::is_directory(alias), "Cannot create the junction fixture");
            struct RemoveJunction { fs::path path; ~RemoveJunction() { RemoveDirectoryW(path.c_str()); } } cleanup{alias};
            auto aliased = options; aliased.job_root = alias;
            WorkerBridge bridge(target, root, aliased);
            bridge.Start({L"desktop", L"result", L"--fixture", L"resolved-final"});
            auto result = Completion(bridge);
            Require(result.diagnostic_code.empty() && result.exit_code == 0 && result.remaining_processes == 0,
                (std::string("Resolved job manifest under an aliased job root was rejected: ") + result.diagnostic_message).c_str()); ++checks;
        }
        {
            auto limits = options; limits.command_timeout = 250ms;
            WorkerBridge bridge(target, root, limits);
            bridge.Start({L"doctor", L"--fixture", L"result-hang"});
            auto result = Completion(bridge);
            Require(result.diagnostic_code == "BRIDGE_TIMEOUT" && result.envelope.is_null(), "Result escaped before process completion"); ++checks;
        }
        {
            WorkerBridge bridge(target, root, options);
            const auto marker = base / L"leaked.json";
            bridge.Start({L"doctor", L"--fixture", L"leak-child", L"--marker", marker.native()});
            auto pids = ReadMarker(marker); Process child(pids["child"].get<DWORD>());
            auto result = Completion(bridge);
            Require(result.diagnostic_code == "BRIDGE_PROCESS_LEAK" && result.remaining_processes == 0 && child.Done(), "Leaked descendant survived"); ++checks;
        }
        {
            WorkerBridge bridge(target, root, options);
            const auto marker = base / L"shutdown.json";
            bridge.Start({L"doctor", L"--fixture", L"hang-child", L"--marker", marker.native()});
            auto pids = ReadMarker(marker); Process main(pids["parent"].get<DWORD>()), child(pids["child"].get<DWORD>());
            Process sentinel(v2d::LaunchDetached({args[0], L"--sleep"}, repository));
            const auto began = std::chrono::steady_clock::now(); bridge.Shutdown(0ms);
            Require(std::chrono::steady_clock::now() - began < 3s && main.Done(1000) && child.Done(1000), "Shutdown did not stop owned descendants promptly");
            Require(!sentinel.Done(), "Shutdown touched an unrelated/detached child"); ++checks;
        }
        {
            const auto marker = base / L"parent-death.json";
            Process parent(v2d::LaunchDetached({args[0], L"--owning-parent", root.native(), options.job_root.native(), marker.native()}, repository));
            auto pids = ReadMarker(marker); Process main(pids["parent"].get<DWORD>()), child(pids["child"].get<DWORD>());
            parent.Kill(); Require(parent.Done(1000) && main.Done(3000) && child.Done(3000), "GUI death left an owned process alive"); ++checks;
        }
        std::cout << checks << " native bridge checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Bridge test failure: " << error.what() << '\n';
        return 1;
    }
}
