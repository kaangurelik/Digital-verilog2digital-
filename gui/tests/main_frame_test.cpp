// Exercise native controls with BM_CLICK, wxCommandEvent and modal hooks.
// These events do not simulate physical mouse or keyboard input.
#include "main_frame.h"
#include "preview_panel.h"
#include "project_document.h"
#include <wx/app.h>
#include <wx/dcclient.h>
#include <wx/dcmemory.h>
#include <wx/filedlg.h>
#include <wx/display.h>
#include <wx/evtloop.h>
#include <wx/modalhook.h>
#include <wx/weakref.h>
#include <windows.h>
#include <shellapi.h>
#include <chrono>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <thread>

namespace fs = std::filesystem;
using Json = nlohmann::json;
using namespace std::chrono_literals;

class MainFrameTestApp final : public wxApp {
public:
    bool OnInit() override { wxInitAllImageHandlers(); SetExitOnFrameDelete(false); return true; }
};
wxIMPLEMENT_APP_NO_MAIN(MainFrameTestApp);

namespace {
void Require(bool ok, const std::string& message) { if (!ok) throw std::runtime_error(message); }
void Pump() {
    auto* loop = wxEventLoopBase::GetActive();
    Require(loop != nullptr, "Native test event loop was not activated");
    for (unsigned dispatched = 0; dispatched < 100 && loop->Pending(); ++dispatched) loop->Dispatch();
    wxTheApp->ProcessPendingEvents(); wxTheApp->ProcessIdle();
    std::this_thread::sleep_for(10ms);
}
void PumpFor(std::chrono::milliseconds duration) {
    const auto end = std::chrono::steady_clock::now() + duration;
    while (std::chrono::steady_clock::now() < end) Pump();
}
std::string Read(const fs::path& path) {
    std::ifstream stream(path, std::ios::binary);
    Require(bool(stream), "Cannot read fixture artifact: " + path.u8string());
    return {std::istreambuf_iterator<char>(stream), {}};
}
void Write(const fs::path& path, const std::string& data) {
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    stream.write(data.data(), static_cast<std::streamsize>(data.size()));
    Require(stream.good(), "Cannot write fixture: " + path.u8string());
}
wxButton* Button(wxWindow& parent, const wxString& text) {
    for (auto* child : parent.GetChildren()) {
        if (auto* button = dynamic_cast<wxButton*>(child))
            if (wxStripMenuCodes(button->GetLabel()) == text) return button;
        if (auto* found = Button(*child, text)) return found;
    }
    return nullptr;
}
PreviewPanel* Preview(wxWindow& parent) {
    for (auto* child : parent.GetChildren()) {
        if (auto* panel = dynamic_cast<PreviewPanel*>(child)) return panel;
        if (auto* found = Preview(*child)) return found;
    }
    return nullptr;
}
wxChoice* ViewChoice(wxWindow& parent) {
    for (auto* child : parent.GetChildren()) {
        if (auto* choice = dynamic_cast<wxChoice*>(child))
            if (choice->FindString("Before refinement") != wxNOT_FOUND) return choice;
        if (auto* found = ViewChoice(*child)) return found;
    }
    return nullptr;
}
class EnvironmentOverride final {
    std::wstring key_; wxString previous_; bool present_;
public:
    EnvironmentOverride(std::wstring key, const fs::path& value) : key_(std::move(key)), present_(wxGetEnv(wxString(key_), &previous_)) {
        Require(SetEnvironmentVariableW(key_.c_str(), value.c_str()) != 0, "Cannot set isolated test environment override");
    }
    ~EnvironmentOverride() { SetEnvironmentVariableW(key_.c_str(), present_ ? previous_.wc_str() : nullptr); }
};
unsigned nativeButtons = 0, menuCommands = 0, nativeKeys = 0, dialogSelections = 0, choiceCommands = 0;
void Click(MainFrame& frame, const wxString& label) {
    auto* button = Button(frame, label);
    Require(button && button->IsEnabled(), "Native button unavailable: " + v2d::Utf8(label));
    SendMessageW(reinterpret_cast<HWND>(button->GetHandle()), BM_CLICK, 0, 0);
    ++nativeButtons;
}
void Menu(MainFrame& frame, int id) {
    Require(id != wxNOT_FOUND && frame.GetMenuBar()->IsEnabled(id), "Menu command unavailable");
    wxCommandEvent event(wxEVT_MENU, id); event.SetEventObject(&frame);
    Require(frame.GetEventHandler()->ProcessEvent(event), "Menu event had no handler");
    ++menuCommands;
}
int MenuId(MainFrame& frame, const wxString& menu, const wxString& item) {
    const int id = frame.GetMenuBar()->FindMenuItem(menu, item);
    Require(id != wxNOT_FOUND, "Menu item not found: " + v2d::Utf8(item)); return id;
}
class Dialogs final : public wxModalDialogHook {
    std::deque<fs::path> paths_;
    std::string failure_;
protected:
    int Enter(wxDialog* dialog) override {
        auto* file = dynamic_cast<wxFileDialog*>(dialog);
        if (!file || paths_.empty()) { failure_ = "Unexpected modal dialog: " + v2d::Utf8(dialog->GetTitle()); return wxID_CANCEL; }
        file->SetPath(v2d::Display(paths_.front())); paths_.pop_front(); ++dialogSelections;
        return wxID_OK;
    }
public:
    Dialogs() { Register(); }
    void Select(const fs::path& path) { Require(paths_.empty(), "Previous dialog expectation was not consumed"); paths_.push_back(path); }
    void Check() const { Require(failure_.empty(), failure_); Require(paths_.empty(), "Expected file dialog was not opened"); }
};
double Wait(MainFrame& frame, const std::string& operation) {
    Require(frame.Busy(), operation + " did not start a worker");
    const auto begin = std::chrono::steady_clock::now();
    while (frame.Busy() && std::chrono::steady_clock::now() - begin < 90s) Pump();
    Require(!frame.Busy(), operation + " exceeded the native acceptance deadline");
    PumpFor(60ms);
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - begin).count();
}
void Ready(const MainFrame& frame, const std::string& operation) {
    const auto state = frame.TestState();
    Require(state.at("stage") == "Ready", operation + ": " + state.at("details").get<std::string>());
}
void Preserved(const MainFrame& frame, const Json& previous, const fs::path& manifest, const std::string& bytes) {
    auto state = frame.TestState();
    Require(state.at("result_directory") == previous.at("result_directory") && state.at("preview_size") == previous.at("preview_size")
        && state.at("metadata") == previous.at("metadata"), "Failed/cancelled action replaced the prior visible result");
    Require(Read(manifest) == bytes, "Failed/cancelled action replaced the published revision");
}
Json Capture(MainFrame& frame, const fs::path& path) {
    frame.ShowWithoutActivating(); frame.Layout(); frame.Refresh(); frame.Update(); PumpFor(400ms);
    Click(frame, "Fit"); frame.Update(); PumpFor(50ms);
    const auto size = frame.GetSize(), client = frame.GetClientSize(), dpi = frame.GetDPI();
    wxBitmap bitmap(size.x, size.y); wxMemoryDC target(bitmap); wxWindowDC source(&frame);
    Require(target.Blit(0, 0, size.x, size.y, &source, 0, 0), "Native window capture failed");
    target.SelectObject(wxNullBitmap);
    Require(bitmap.ConvertToImage().SaveFile(v2d::Display(path), wxBITMAP_TYPE_PNG), "Cannot save native UI screenshot");
    return {{"path", path.u8string()}, {"window_pixels", {size.x, size.y}}, {"client_pixels", {client.x, client.y}},
        {"dpi", {dpi.x, dpi.y}}, {"dpi_scale", frame.GetDPIScaleFactor()}, {"resize_only", true}};
}
Json Screenshot(MainFrame& frame, const fs::path& path, bool small) {
    if (small) frame.SetClientSize(850, 560); else frame.SetSize(1250, 840);
    return Capture(frame, path);
}
void Fonts(wxWindow& window, std::vector<std::pair<wxWindow*, wxFont>>& originals) {
    originals.emplace_back(&window, window.GetFont());
    for (auto* child : window.GetChildren()) Fonts(*child, originals);
}
Json FontStress(MainFrame& frame, const fs::path& directory) {
    std::vector<std::pair<wxWindow*, wxFont>> originals; Fonts(frame, originals);
    Json results = Json::array();
    for (double scale : {1.5, 2.0}) {
        for (const auto& item : originals) {
            auto font = item.second; font.SetFractionalPointSize(font.GetFractionalPointSize() * scale);
            item.first->SetFont(font); item.first->InvalidateBestSize();
        }
        frame.SetSize(frame.FromDIP(wxSize(static_cast<int>(1250 * scale), static_cast<int>(840 * scale))));
        frame.Layout();
        auto capture = Capture(frame, directory / (scale == 1.5 ? "font-150-percent.png" : "font-200-percent.png"));
        capture["font_scale"] = scale; capture["real_DPI_transition"] = false; results.push_back(capture);
    }
    for (const auto& item : originals) { item.first->SetFont(item.second); item.first->InvalidateBestSize(); }
    frame.SetSize(1250, 840); frame.Layout(); return results;
}
class OwnedDigital final {
public:
    const DWORD pid;
    HANDLE process;
    HWND window = nullptr;
    HWND visibleFallback = nullptr;
    Json observed = Json::array();
    explicit OwnedDigital(DWORD identifier) : pid(identifier), process(OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid)) {
        Require(process != nullptr, "Cannot bind the explicitly launched Digital process");
    }
    ~OwnedDigital() {
        if (process) {
            if (WaitForSingleObject(process, 0) == WAIT_TIMEOUT) TerminateProcess(process, 1);
            WaitForSingleObject(process, 3000); CloseHandle(process);
        }
    }
    void FindWindow() {
        observed = Json::array(); visibleFallback = nullptr;
        EnumWindows([](HWND candidate, LPARAM context) -> BOOL {
            auto& self = *reinterpret_cast<OwnedDigital*>(context); DWORD owner = 0;
            GetWindowThreadProcessId(candidate, &owner);
            if (owner == self.pid) {
                wchar_t title[32768]{}; GetWindowTextW(candidate, title, 32768);
                wchar_t type[256]{}; GetClassNameW(candidate, type, 256);
                const bool visible = IsWindowVisible(candidate) != 0;
                if (self.observed.size() < 32) self.observed.push_back({{"title", v2d::Utf8(wxString(title))}, {"class", v2d::Utf8(wxString(type))}, {"visible", visible}});
                if (visible) {
                    self.visibleFallback = candidate;
                    if (std::wstring(title).find(L"circuit.dig") != std::wstring::npos) { self.window = candidate; return FALSE; }
                }
            }
            return TRUE;
        }, reinterpret_cast<LPARAM>(this));
    }
    std::wstring Title() const { wchar_t text[32768]{}; GetWindowTextW(window, text, 32768); return text; }
    Json Close() {
        DWORD owner = 0; GetWindowThreadProcessId(window, &owner);
        Require(owner == pid, "Digital window ownership changed before close");
        Require(PostMessageW(window, WM_CLOSE, 0, 0) != 0, "Cannot close the owned Digital window");
        const auto deadline = std::chrono::steady_clock::now() + 5s;
        while (WaitForSingleObject(process, 0) == WAIT_TIMEOUT && std::chrono::steady_clock::now() < deadline) Pump();
        bool forced = WaitForSingleObject(process, 0) == WAIT_TIMEOUT;
        if (forced) Require(TerminateProcess(process, 1) != 0, "Cannot stop the owned Digital process after its close deadline");
        Require(WaitForSingleObject(process, 3000) == WAIT_OBJECT_0, "Owned Digital process survived cleanup");
        return {{"forced", forced}, {"process_exited", true}};
    }
};
void CaptureOwnedWindow(HWND window, const fs::path& path) {
    RECT bounds{}; Require(GetWindowRect(window, &bounds) != 0, "Cannot read the owned Digital window bounds");
    const int width = bounds.right - bounds.left, height = bounds.bottom - bounds.top;
    Require(width > 0 && height > 0 && static_cast<long long>(width) * height <= 16000000, "Digital capture dimensions exceed test bound");
    wxBitmap bitmap(width, height); wxMemoryDC capture(bitmap); HDC source = GetWindowDC(window);
    Require(source != nullptr, "Cannot capture the owned Digital window");
    const bool copied = BitBlt(static_cast<HDC>(capture.GetHDC()), 0, 0, width, height, source, 0, 0, SRCCOPY) != 0;
    ReleaseDC(window, source); capture.SelectObject(wxNullBitmap);
    Require(copied && bitmap.ConvertToImage().SaveFile(v2d::Display(path), wxBITMAP_TYPE_PNG), "Cannot save the owned Digital window capture");
}
Json DigitalJourney(MainFrame*& frame, const fs::path& directory) {
    Click(*frame, "Open in Digital"); Wait(*frame, "checked Digital launch"); Ready(*frame, "checked Digital launch");
    const auto status = v2d::Utf8(frame->GetStatusBar()->GetStatusText());
    const auto first = status.find("process "), last = status.find(").");
    Require(first != std::string::npos && last > first + 8, "Digital launch did not return an explicit process ID");
    const auto number = std::stoul(status.substr(first + 8, last - first - 8));
    Require(number > 0 && number <= MAXDWORD, "Invalid launched Digital process ID");
    OwnedDigital digital(static_cast<DWORD>(number));
    wchar_t executable[32768]{}; DWORD capacity = 32768;
    Require(QueryFullProcessImageNameW(digital.process, 0, executable, &capacity) != 0
        && fs::path(executable).filename() == L"javaw.exe", "Launched process is not the checked Java GUI runtime");
    const auto deadline = std::chrono::steady_clock::now() + 20s;
    while (!digital.window && std::chrono::steady_clock::now() < deadline) {
        Require(WaitForSingleObject(digital.process, 0) == WAIT_TIMEOUT, "Digital exited before loading the circuit");
        digital.FindWindow(); Pump();
    }
    if (!digital.window) {
        Write(directory / "digital-observed-windows.json", digital.observed.dump(2) + "\n");
        if (digital.visibleFallback) CaptureOwnedWindow(digital.visibleFallback, directory / "digital-error-window.png");
        throw std::runtime_error("Digital did not display the generated circuit within its startup bound: " + digital.observed.dump());
    }
    const auto title = digital.Title();
    Require(title.find(L"Digital") != std::wstring::npos, "Generated circuit window lacks the Digital title");
    PumpFor(500ms);
    CaptureOwnedWindow(digital.window, directory / "digital-window.png");
    wxWeakRef<MainFrame> weak(frame); frame->Close(true); frame = nullptr;
    const auto closeDeadline = std::chrono::steady_clock::now() + 4s;
    while (weak && std::chrono::steady_clock::now() < closeDeadline) Pump();
    Require(!weak && WaitForSingleObject(digital.process, 0) == WAIT_TIMEOUT && IsWindowVisible(digital.window),
        "Explicitly opened Digital did not survive conversion GUI closure");
    return {{"name", "open_digital_native_button"}, {"pid", digital.pid}, {"title", v2d::Utf8(wxString(title))},
        {"screenshot", (directory / "digital-window.png").u8string()}, {"survived_GUI_close", true}, {"cleanup", digital.Close()}, {"valid", true}};
}
}

int main(int argc, char** argv) {
    int wideCount = 0; auto** raw = CommandLineToArgvW(GetCommandLineW(), &wideCount);
    std::vector<std::wstring> wideArgs(raw, raw + wideCount); LocalFree(raw);
    if (!wxEntryStart(argc, argv)) return 2;
    if (!wxTheApp->CallOnInit()) { wxEntryCleanup(); return 2; }
    wxGUIEventLoop nativeLoop; wxEventLoopActivator active(&nativeLoop);
    int result = 0; MainFrame* frame = nullptr; Json evidence = {{"valid", false}, {"operations", Json::array()}};
    fs::path directory;
    try {
        const bool digitalOnly = wideArgs.size() == 4 && wideArgs[2] == L"--open-digital-only";
        Require(wideArgs.size() == 2 || digitalOnly, "Usage: v2d-main-frame-tests <repository-root> [--open-digital-only <manifest>]");
        const auto repository = fs::absolute(wideArgs[1]);
        directory = repository / L".runs/gui-main-frame-native" / (std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64())) / L"hdl_\u0131\u015f\u0131k_\u03c0";
        fs::create_directories(directory);
        if (digitalOnly) {
            frame = new MainFrame(repository); wxTheApp->SetTopWindow(frame); Dialogs dialogs;
            dialogs.Select(fs::path(wideArgs[3])); Menu(*frame, MenuId(*frame, "File", "Reopen circuit package...")); dialogs.Check();
            Wait(*frame, "reopen for Digital launch"); Ready(*frame, "reopen for Digital launch");
            // Inject a diagnostic while retaining valid artifacts for reopen/display.
            // Python tests cover actual missing-model failures.
            const auto original = fs::u8path(frame->TestState()["result_directory"].get<std::string>());
            const auto diagnosticDirectory = directory / "recorded-model-missing";
            fs::create_directory(diagnosticDirectory);
            auto diagnosticManifest = v2d::ReadJson(original / "manifest.json", 8 * 1024 * 1024);
            for (const auto& artifact : diagnosticManifest.at("artifacts")) {
                const auto relative = fs::u8path(artifact.at("path").get<std::string>());
                Require(!relative.is_absolute() && relative.generic_u8string().find("..") == std::string::npos, "Unsafe accepted fixture artifact path");
                fs::create_directories((diagnosticDirectory / relative).parent_path());
                fs::copy_file(original / relative, diagnosticDirectory / relative);
            }
            diagnosticManifest["refinement"] = {{"mode", "local"}, {"accepted", 0}, {"canvases", Json::array({
                {{"attempts", Json::array({{{"diagnostic", {{"code", "MODEL_MISSING"}, {"message", "Install the pinned Qwen3 model in Models, then retry optional refinement."}}}}})}}
            })}};
            Write(diagnosticDirectory / "manifest.json", diagnosticManifest.dump(2) + "\n");
            dialogs.Select(diagnosticDirectory / "manifest.json"); Menu(*frame, MenuId(*frame, "File", "Reopen circuit package...")); dialogs.Check();
            Wait(*frame, "recorded missing-model display"); Ready(*frame, "recorded missing-model display");
            const auto diagnosticState = frame->TestState(); const auto detail = diagnosticState.at("details").get<std::string>();
            Require(detail.find("MODEL_MISSING") != std::string::npos && detail.find("Install the pinned") != std::string::npos
                && diagnosticState.at("metadata") == true && diagnosticState.at("preview_size")[0].get<int>() > 0,
                "Recorded model unavailability was hidden or invalidated the valid circuit");
            evidence["operations"].push_back({{"name", "recorded_missing_model_diagnostic"}, {"synthetic_recorded_diagnostic", true}, {"native_artifacts_unchanged", true}, {"valid", true}});
            evidence["screenshots"] = Json::array({Screenshot(*frame, directory / "fitted-window-1250x840.png", false),
                Screenshot(*frame, directory / "fitted-window-client-850x560.png", true)});
            evidence["displays"] = Json::array();
            for (unsigned index = 0; index < wxDisplay::GetCount(); ++index) {
                wxDisplay display(index); const auto geometry = display.GetGeometry(), usable = display.GetClientArea(); const auto ppi = display.GetPPI();
                evidence["displays"].push_back({{"index", index}, {"name", v2d::Utf8(display.GetName())}, {"geometry", {geometry.x, geometry.y, geometry.width, geometry.height}},
                    {"usable", {usable.width, usable.height}}, {"PPI", {ppi.x, ppi.y}}, {"scale", display.GetScaleFactor()}});
            }
            evidence["font_stress"] = FontStress(*frame, directory); frame->Hide();
            evidence["operations"].push_back(DigitalJourney(frame, directory));
            evidence["valid"] = true;
        } else {
        const auto source = directory / L"mod\u00fcl.v", project = directory / L"project.v2d.json";
        const std::string hdl = "module gui_buffer(input [3:0] a, output [3:0] y); assign y = ~a; endmodule\n";
        Write(source, hdl);
        frame = new MainFrame(repository); wxTheApp->SetTopWindow(frame);
        Dialogs dialogs;
        frame->AddSources({v2d::Display(source), v2d::Display(source)});
        Require(frame->TestState()["sources"] == 1, "Source import did not preserve a unique compilation list");
        dialogs.Select(project); Menu(*frame, wxID_SAVE); dialogs.Check();
        Require(fs::is_regular_file(project) && !frame->TestState()["dirty"].get<bool>(), "Save project failed");
        auto saved = v2d::ReadJson(project);
        Require(!fs::u8path(saved["sources"][0].get<std::string>()).is_absolute(), "Project save leaked absolute source paths");
        dialogs.Select(project); Menu(*frame, wxID_OPEN); dialogs.Check();
        Require(frame->TestState()["sources"] == 1 && frame->TestState()["top"] == "", "Project reopen lost source/top state");
        evidence["operations"].push_back({{"name", "source_import_save_reopen"}, {"valid", true}});

        Click(*frame, "Inspect tops");
        const auto inspectSeconds = Wait(*frame, "inspect"); Ready(*frame, "inspect");
        Require(frame->TestState()["top"] == "gui_buffer", "Native inspection did not populate the discovered top");
        Menu(*frame, wxID_SAVE); dialogs.Check();
        Require(v2d::ReadJson(project)["top"] == "gui_buffer", "Discovered top did not round-trip through Save");
        evidence["operations"].push_back({{"name", "inspect_discover_top"}, {"seconds", inspectSeconds}, {"valid", true}});

        unsigned heartbeats = 0;
        wxEvtHandler beatTarget; beatTarget.Bind(wxEVT_TIMER, [&](wxTimerEvent&) { ++heartbeats; });
        wxTimer beat(&beatTarget); beat.Start(25);
        Click(*frame, "Convert");
        Require(!Button(*frame, "Convert")->IsEnabled() && Button(*frame, "Cancel")->IsEnabled(), "Busy state did not lock inputs and expose Cancel");
        const auto convertSeconds = Wait(*frame, "convert and checked preview"); beat.Stop(); Ready(*frame, "convert");
        const auto selected = frame->TestState();
        Require(selected["metadata"].get<bool>() && selected["preview_size"][0].get<int>() > 0 && selected["preview_size"][1].get<int>() > 0,
            "Conversion did not reach the checked native preview");
        evidence["conversion_heartbeat_events"] = heartbeats;
        Require(heartbeats >= 5, "Main wx event loop did not remain responsive during conversion");
        const auto resultDirectory = fs::u8path(selected["result_directory"].get<std::string>());
        const auto manifest = resultDirectory / "manifest.json"; const auto manifestBytes = Read(manifest);
        auto publication = Json::parse(manifestBytes);
        Require(publication.at("state") == "ready", "GUI displayed a non-ready publication");
        evidence["operations"].push_back({{"name", "convert_checked_preview"}, {"seconds", convertSeconds}, {"heartbeat_events", heartbeats}, {"valid", true}});

        auto* preview = Preview(*frame); Require(preview != nullptr, "Native preview window missing");
        const auto beforeZoom = preview->GetZoom();
        SendMessageW(reinterpret_cast<HWND>(preview->GetHandle()), WM_KEYDOWN, VK_ADD, 1);
        SendMessageW(reinterpret_cast<HWND>(preview->GetHandle()), WM_KEYUP, VK_ADD, 1); ++nativeKeys;
        Require(preview->GetZoom() > beforeZoom, "Native preview key notification did not zoom");
        evidence["screenshots"] = Json::array({Screenshot(*frame, directory / "window-1250x840.png", false),
            Screenshot(*frame, directory / "window-client-850x560.png", true)});
        frame->Hide();

        Click(*frame, "Check tools and memory"); const auto doctorSeconds = Wait(*frame, "doctor"); Ready(*frame, "doctor");
        Require(frame->TestState()["details"].get<std::string>().find("Runtime diagnostics") != std::string::npos, "Doctor output was not presented");
        Preserved(*frame, selected, manifest, manifestBytes);
        Click(*frame, "Status"); Wait(*frame, "model inventory"); Ready(*frame, "model inventory");
        Require(frame->TestState()["details"].get<std::string>().find("qwen3-4b-q4_k_m: present") != std::string::npos, "Installed Q4 model was not listed");
        Click(*frame, "Verify"); const auto verifySeconds = Wait(*frame, "model verification"); Ready(*frame, "model verification");
        Require(frame->TestState()["details"].get<std::string>().find("hashes verified") != std::string::npos, "Model verification did not verify pinned hashes");
        Preserved(*frame, selected, manifest, manifestBytes);
        evidence["operations"].push_back({{"name", "doctor_and_model_inventory_verification"}, {"doctor_seconds", doctorSeconds}, {"verify_seconds", verifySeconds}, {"valid", true}});

        const bool portableRelease = fs::is_regular_file(repository / "release-manifest.json");
        {
            EnvironmentOverride missingJava(L"V2D_JAVA", directory / "missing-java.exe");
            // Packaged tools ignore checkout overrides; release tests verify private paths.
            Click(*frame, portableRelease ? "Check tools and memory" : "Convert");
        }
        if (portableRelease) {
            Wait(*frame, "release ignores external Java override"); Ready(*frame, "release private Java doctor");
            Require(frame->TestState()["details"].get<std::string>().find("java: available") != std::string::npos,
                "Release doctor used the invalid external Java override");
        } else {
            Wait(*frame, "missing Java failure");
            Require(frame->TestState()["stage"] == "Operation failed", "Missing Java did not produce a visible error");
        }
        Preserved(*frame, selected, manifest, manifestBytes);
        evidence["operations"].push_back({{"name", portableRelease ? "release_ignores_external_tool_override" : "missing_tool_preserves_prior"},
            {"private_tool_path_checked_by_release_runner", portableRelease}, {"valid", true}});

        Write(source, "module gui_buffer(input a, output y); assign y = ; endmodule\n");
        Click(*frame, "Convert"); const auto failureSeconds = Wait(*frame, "invalid HDL");
        Require(frame->TestState()["stage"] == "Operation failed", "Invalid HDL did not produce a visible error");
        Preserved(*frame, selected, manifest, manifestBytes); Write(source, hdl);
        evidence["operations"].push_back({{"name", "failed_conversion_preserves_prior"}, {"seconds", failureSeconds}, {"valid", true}});

        Click(*frame, "Convert"); Click(*frame, "Cancel");
        const auto cancelSeconds = Wait(*frame, "pre-job-ID cancellation");
        Require(frame->TestState()["stage"] == "Cancelled" || frame->TestState()["stage"] == "Operation failed", "Cancelled conversion was presented as ready");
        Preserved(*frame, selected, manifest, manifestBytes);
        evidence["operations"].push_back({{"name", "cancel_preserves_prior"}, {"seconds", cancelSeconds}, {"valid", true}});

        // A stale wake must not revive a completed action or replace its preview.
        wxQueueEvent(frame, new wxThreadEvent(v2d::EVT_V2D_WORKER)); PumpFor(50ms);
        Require(!frame->Busy(), "Stale mailbox wake restarted a completed action");
        Preserved(*frame, selected, manifest, manifestBytes);

        dialogs.Select(manifest); Menu(*frame, MenuId(*frame, "File", "Reopen circuit package...")); dialogs.Check();
        const auto reopenSeconds = Wait(*frame, "reopen checked package"); Ready(*frame, "reopen");
        Preserved(*frame, selected, manifest, manifestBytes);
        evidence["operations"].push_back({{"name", "reopen_checked_package"}, {"seconds", reopenSeconds}, {"valid", true}});

        const auto exported = directory / L"exported_\u03c0";
        dialogs.Select(exported); Click(*frame, "Export package..."); dialogs.Check();
        const auto exportSeconds = Wait(*frame, "export complete package"); Ready(*frame, "export");
        Require(Read(exported / "manifest.json") == manifestBytes, "Export rewrote the checked manifest");
        for (const auto& artifact : publication.at("artifacts")) {
            auto relative = fs::u8path(artifact.at("path").get<std::string>());
            Require(Read(exported / relative) == Read(resultDirectory / relative), "Export changed an artifact");
        }
        Preserved(*frame, selected, manifest, manifestBytes);
        evidence["operations"].push_back({{"name", "export_complete_package"}, {"seconds", exportSeconds}, {"files", publication.at("artifacts").size() + 1}, {"valid", true}});
        dialogs.Select(exported / "manifest.json"); Menu(*frame, MenuId(*frame, "File", "Reopen circuit package...")); dialogs.Check();
        Wait(*frame, "relocated package reopen"); Ready(*frame, "relocated package reopen");
        Require(fs::u8path(frame->TestState()["result_directory"].get<std::string>()) == exported, "Reopen did not select the exported package");

        const auto previousExport = frame->TestState(); const auto bad = directory / "corrupt-package";
        fs::create_directory(bad);
        for (const auto& item : fs::recursive_directory_iterator(exported)) {
            const auto target = bad / item.path().lexically_relative(exported);
            if (item.is_directory()) fs::create_directory(target); else fs::copy_file(item.path(), target);
        }
        const auto previewFile = bad / fs::u8path(publication["artifacts"]["preview"]["path"].get<std::string>());
        Write(previewFile, Read(previewFile) + "changed");
        dialogs.Select(bad / "manifest.json"); Menu(*frame, MenuId(*frame, "File", "Reopen circuit package...")); dialogs.Check();
        Wait(*frame, "corrupt package rejection");
        Require(frame->TestState()["stage"] == "Operation failed", "Corrupt package was displayed as verified");
        Preserved(*frame, previousExport, exported / "manifest.json", manifestBytes);
        evidence["operations"].push_back({{"name", "corrupt_reopen_preserves_prior"}, {"valid", true}});

        // Run one local Q4 request; require valid HDL/native results whether
        // the proposal is accepted or the baseline is retained.
        Write(source, "module gui_buffer(input a,b,cin, output sum,cout); assign sum=a^b^cin; assign cout=(a&b)|(cin&(a^b)); endmodule\n");
        auto modelProject = v2d::ReadJson(project);
        modelProject["output"] = "model-generated";
        modelProject["conversion"]["refinement"]["mode"] = "local";
        modelProject["conversion"]["refinement"]["model_id"] = "qwen3-4b-q4_k_m";
        modelProject["conversion"]["refinement"]["max_attempts"] = 1;
        modelProject["conversion"]["refinement"]["budget_seconds"] = 30;
        modelProject["conversion"]["refinement"]["context_tokens"] = 4096;
        const auto modelProjectPath = directory / "model-project.v2d.json";
        v2d::WriteJson(modelProjectPath, modelProject, false); frame->OpenProject(modelProjectPath);
        Click(*frame, "Convert"); const auto modelSeconds = Wait(*frame, "local Q4 refinement"); Ready(*frame, "local Q4 refinement");
        const auto modelState = frame->TestState();
        const auto modelDirectory = fs::u8path(modelState["result_directory"].get<std::string>());
        const auto modelManifest = v2d::ReadJson(modelDirectory / "manifest.json", 8 * 1024 * 1024);
        const auto& runtime = modelManifest.at("refinement").at("runtime");
        Require(runtime.at("requests").get<int>() >= 1 && runtime.at("starts").get<int>() >= 1, "Local refinement did not make an actual model request");
        Require(runtime.at("process").at("active_processes") == 0 && runtime.at("process").at("closed") == true, "Model worker survived GUI conversion");
        const auto& validationArtifact = modelManifest.at("artifacts").at("validation");
        const auto validation = v2d::ReadJson(modelDirectory / fs::u8path(validationArtifact.at("path").get<std::string>()), 8 * 1024 * 1024);
        Require(validation.at("behavior").at("valid") == true && modelState.at("baseline") == true && modelState.at("metadata") == true,
            "Model conversion lost original-HDL validation or comparison preview");
        auto* view = ViewChoice(*frame); Require(view && view->IsEnabled(), "Baseline selector unavailable");
        for (const int index : {1, 0}) {
            view->SetSelection(index); wxCommandEvent event(wxEVT_CHOICE, view->GetId()); event.SetEventObject(view);
            Require(view->GetEventHandler()->ProcessEvent(event), "Baseline choice had no handler"); ++choiceCommands;
            Require(Preview(*frame)->IsShowingBaseline() == bool(index), "Baseline selector did not change the native preview");
        }
        evidence["operations"].push_back({{"name", "real_Q4_refinement_and_baseline"}, {"seconds", modelSeconds},
            {"requests", runtime.at("requests")}, {"accepted", modelManifest.at("refinement").at("accepted")},
            {"runtime_process", runtime.at("process")}, {"valid", true}});

        Click(*frame, "Convert"); wxWeakRef<MainFrame> weak(frame);
        const auto closing = std::chrono::steady_clock::now(); frame->Close(true); frame = nullptr;
        while (weak && std::chrono::steady_clock::now() - closing < 5s) Pump();
        Require(!weak, "Close left the native window or owned bridge running");
        const auto closeSeconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - closing).count();
        Require(closeSeconds < 4, "Close exceeded the bounded shutdown grace");
        dialogs.Check();
        evidence["operations"].push_back({{"name", "close_during_job"}, {"seconds", closeSeconds}, {"valid", true}});
        evidence["result_directory"] = resultDirectory.u8string(); evidence["exported_directory"] = exported.u8string();
        evidence["valid"] = true;
        }
        evidence["input"] = {{"native_BM_CLICK", nativeButtons}, {"native_key_messages", nativeKeys}, {"generated_wx_menu_events", menuCommands},
            {"modal_path_hooks", dialogSelections}, {"generated_wx_choice_events", choiceCommands}, {"physical_input_simulated", false}, {"foreground_requested", false}};
        std::cout << evidence["operations"].size() << " native mainframe workflows passed\n";
    } catch (const std::exception& error) {
        evidence["error"] = error.what();
        if (frame) evidence["last_state"] = frame->TestState();
        std::cerr << "Mainframe acceptance failure: " << error.what() << '\n'; result = 1;
    }
    if (frame) { delete frame; frame = nullptr; }
    if (!directory.empty()) {
        Write(directory / "summary.json", evidence.dump(2) + "\n");
        std::cout << "Evidence: " << directory.u8string() << '\n';
    }
    wxTheApp->OnExit(); wxEntryCleanup(); return result;
}
