#pragma once

#include <wx/event.h>
#include <nlohmann/json.hpp>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

namespace v2d {

wxDECLARE_EVENT(EVT_V2D_WORKER, wxThreadEvent);

struct BridgeOptions {
    // Empty preserves inherited V2D_JOB_ROOT, otherwise uses root/.runs/jobs.
    // All native job storage must have an ASCII absolute path.
    std::filesystem::path job_root;
    std::chrono::milliseconds command_timeout{330000};
    std::chrono::milliseconds cancellation_grace{5000};
    std::size_t max_line_bytes = 8 * 1024 * 1024;
    std::size_t max_stdout_bytes = 16 * 1024 * 1024;
    std::size_t max_stderr_bytes = 1024 * 1024;
    std::size_t stderr_tail_bytes = 64 * 1024;
};

struct WorkerUpdate {
    enum class Kind { Progress, Completed };
    Kind kind = Kind::Progress;
    std::uint64_t generation = 0;
    // Validated CLI envelope, or null on process/protocol failure.
    // Progress updates may be coalesced.
    nlohmann::json envelope;
    std::string diagnostic_code;
    std::string diagnostic_message;
    std::string stderr_tail;
    std::uint32_t exit_code = 0;
    std::uint32_t remaining_processes = 0;
    std::size_t stdout_bytes = 0;
    std::size_t stderr_bytes = 0;
    std::size_t coalesced_progress = 0;
    bool cancellation_requested = false;
    bool forced = false;
};

// Call Start/DrainEvents on the UI thread; the target must outlive the bridge.
// Shutdown detaches delivery. Wakes carry no payload: drain the mailbox and
// match each update's generation to the active request.
class WorkerBridge final {
public:
    WorkerBridge(wxEvtHandler& target, std::filesystem::path repositoryRoot,
                 BridgeOptions options = {});
    ~WorkerBridge();
    WorkerBridge(const WorkerBridge&) = delete;
    WorkerBridge& operator=(const WorkerBridge&) = delete;

    // Pass v2d arguments; the bridge supplies .venv Python and --events.
    // Throws on invalid configuration, a busy bridge or launch failure.
    std::uint64_t Start(const std::vector<std::wstring>& cliArgs);
    void Cancel();
    void Shutdown(std::chrono::milliseconds grace = std::chrono::milliseconds{1500});
    bool Running() const;
    std::vector<WorkerUpdate> DrainEvents();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// Launch an explicit Open action after Python validates its arguments.
// Digital runs independently and can outlive the GUI.
std::uint32_t LaunchDetached(const std::vector<std::wstring>& checkedArgv,
                             const std::filesystem::path& cwd);

}  // namespace v2d
