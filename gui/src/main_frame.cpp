#include "main_frame.h"
#include "preview_panel.h"
#include "project_document.h"
#include <wx/dirdlg.h>
#include <wx/filedlg.h>
#include <wx/dnd.h>
#include <wx/scrolwin.h>
#include <wx/splitter.h>
#include <wx/tokenzr.h>
#include <algorithm>
#include <cmath>
#include <sstream>

using json = nlohmann::json;
using namespace v2d;
namespace {
enum { IdInspect = wxID_HIGHEST + 100, IdConvert, IdCancel, IdOpenResult, IdExport, IdDigital,
       IdDoctor, IdAddSource, IdSaveAs, IdModels, IdVerifyModel, IdInstallModel };
const char* models[] = {"qwen3-4b-q4_k_m", "qwen3-4b-q5_k_m", "qwen3-14b-q4_k_m"};
const char* profiles[] = {"auto", "scalar-v1", "datapath-v1", "sequential-v1"};
class SourceDrop final : public wxFileDropTarget {
    MainFrame& frame_;
public:
    explicit SourceDrop(MainFrame& frame) : frame_(frame) {}
    bool OnDropFiles(wxCoord, wxCoord, const wxArrayString& files) override {
        if (frame_.Busy()) return false;
        frame_.AddSources(files); return true;
    }
};
wxArrayString Lines(const wxString& text) {
    wxArrayString result;
    wxStringTokenizer tokens(text, "\r\n", wxTOKEN_STRTOK);
    while (tokens.HasMoreTokens()) { auto line = tokens.GetNextToken(); line.Trim().Trim(false); if (!line.empty()) result.Add(line); }
    return result;
}
json Definitions(const wxString& text) {
    auto result = json::object();
    for (auto line : Lines(text)) {
        auto split = line.Find('=');
        if (split <= 0) throw std::runtime_error("Defines and parameters use one NAME=value per line");
        auto name = Utf8(line.Left(split).Trim().Trim(false));
        if (result.contains(name)) throw std::runtime_error("Repeated definition: " + name);
        result[name] = Utf8(line.Mid(split + 1));
    }
    return result;
}
wxString DefinitionText(const json& object) {
    wxString result;
    for (const auto& item : object.items()) result += Wide(item.key()) + "=" +
        Wide(item.value().is_string() ? item.value().get<std::string>() : item.value().dump()) + "\n";
    return result;
}
wxString ListText(const json& list) {
    wxString result;
    for (const auto& item : list) result += Wide(item.get<std::string>()) + "\n";
    return result;
}
wxString Message(const json& diagnostic) {
    wxString text = Wide(diagnostic.value("code", "ERROR")) + ": " + Wide(diagnostic.value("message", "The operation failed"));
    if (diagnostic.contains("path")) {
        text += "\n" + Wide(diagnostic["path"].get<std::string>());
        if (diagnostic.contains("line")) text += ":" + Wide(diagnostic["line"].dump());
        if (diagnostic.contains("column")) text += ":" + Wide(diagnostic["column"].dump());
    }
    return text;
}
}

MainFrame::MainFrame(std::filesystem::path root)
    : wxFrame(nullptr, wxID_ANY, "Verilog to Digital", wxDefaultPosition, wxSize(1250, 840)),
      root_(std::filesystem::absolute(root)), pulse_(this) {
    SetMinClientSize(FromDIP(wxSize(850, 560)));
    BuildInterface(); Menus();
    Bind(EVT_V2D_WORKER, &MainFrame::OnWorker, this);
    Bind(wxEVT_TIMER, [this](wxTimerEvent&) { if (Busy()) progress_->Pulse(); });
    Bind(wxEVT_CLOSE_WINDOW, &MainFrame::OnClose, this);
    SetDropTarget(new SourceDrop(*this));
    ApplyProject({{"schema_version", 1}, {"sources", json::array()}});
    CreateStatusBar(); SetStatusText("Add Verilog files or open a project to begin.");
    UpdateEnabled();
}
MainFrame::~MainFrame() { if (bridge_) bridge_->Shutdown(); }
bool MainFrame::Busy() const { return action_ != Action::None; }

void MainFrame::BuildInterface() {
    auto* outer = new wxBoxSizer(wxVERTICAL);
    auto* commands = new wxBoxSizer(wxHORIZONTAL);
    auto button = [&](wxWindow* parent, wxSizer* sizer, const wxString& title, int id, auto handler) {
        auto* control = new wxButton(parent, id, title);
        control->Bind(wxEVT_BUTTON, handler); sizer->Add(control, 0, wxRIGHT, FromDIP(6));
        idleControls_.push_back(control); return control;
    };
    button(this, commands, "Add &Verilog...", IdAddSource, [this](wxCommandEvent&) {
        wxFileDialog dialog(this, "Add Verilog sources in compilation order", "", "", "Verilog (*.v)|*.v|All files|*.*", wxFD_OPEN | wxFD_FILE_MUST_EXIST | wxFD_MULTIPLE);
        if (dialog.ShowModal() == wxID_OK) { wxArrayString files; dialog.GetPaths(files); AddSources(files); }
    });
    button(this, commands, "&Inspect tops", IdInspect, [this](wxCommandEvent&) { RunProject(true); });
    convert_ = button(this, commands, "&Convert", IdConvert, [this](wxCommandEvent&) { RunProject(false); });
    cancel_ = new wxButton(this, IdCancel, "Cancel");
    cancel_->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { if (bridge_) { bridge_->Cancel(); stage_->SetLabel("Cancelling and closing owned workers..."); cancel_->Disable(); } });
    commands->Add(cancel_, 0, wxRIGHT, FromDIP(6)); commands->AddStretchSpacer();
    export_ = button(this, commands, "Export package...", IdExport, [this](wxCommandEvent&) { ExportResult(); });
    digital_ = button(this, commands, "Open in Digital", IdDigital, [this](wxCommandEvent&) {
        if (!resultDirectory_.empty()) Begin(Action::Digital, {L"desktop", L"digital", resultDirectory_.wstring()});
    });
    outer->Add(commands, 0, wxEXPAND | wxALL, FromDIP(10));
    auto* splitter = new wxSplitterWindow(this, wxID_ANY, wxDefaultPosition, wxDefaultSize, wxSP_LIVE_UPDATE | wxSP_3D);
    splitter->SetMinimumPaneSize(FromDIP(260)); splitter->SetSashGravity(0.0);
    auto* notebook = new wxNotebook(splitter, wxID_ANY);
    auto page = [&](const wxString& title) {
        auto* panel = new wxScrolledWindow(notebook); panel->SetScrollRate(0, FromDIP(12));
        panel->SetSizer(new wxBoxSizer(wxVERTICAL)); notebook->AddPage(panel, title); return panel;
    };
    auto label = [&](wxWindow* parent, const wxString& title) { parent->GetSizer()->Add(new wxStaticText(parent, wxID_ANY, title), 0, wxLEFT | wxRIGHT | wxTOP, FromDIP(10)); };
    auto text = [&](wxWindow* parent, const wxString& title, bool multi = false) {
        label(parent, title);
        auto* control = new wxTextCtrl(parent, wxID_ANY, "", wxDefaultPosition, multi ? FromDIP(wxSize(-1, 70)) : wxDefaultSize, multi ? wxTE_MULTILINE : 0);
        control->SetMaxLength(multi ? 65536 : 32760);
        parent->GetSizer()->Add(control, 0, wxEXPAND | wxALL, FromDIP(10)); idleControls_.push_back(control);
        control->Bind(wxEVT_TEXT, [this](wxCommandEvent&) { Changed(); }); return control;
    };
    auto choice = [&](wxWindow* parent, const wxString& title, const wxArrayString& values) {
        label(parent, title); auto* control = new wxChoice(parent, wxID_ANY, wxDefaultPosition, wxDefaultSize, values);
        control->SetSelection(0); parent->GetSizer()->Add(control, 0, wxEXPAND | wxALL, FromDIP(10)); idleControls_.push_back(control);
        control->Bind(wxEVT_CHOICE, [this](wxCommandEvent&) { Changed(); }); return control;
    };
    auto check = [&](wxWindow* parent, const wxString& title) {
        auto* control = new wxCheckBox(parent, wxID_ANY, title); parent->GetSizer()->Add(control, 0, wxALL, FromDIP(10)); idleControls_.push_back(control);
        control->Bind(wxEVT_CHECKBOX, [this](wxCommandEvent&) { Changed(); }); return control;
    };
    auto spin = [&](wxWindow* parent, const wxString& title, int minimum, int maximum, int initial) {
        auto* row = new wxBoxSizer(wxHORIZONTAL); row->Add(new wxStaticText(parent, wxID_ANY, title), 1, wxALIGN_CENTER_VERTICAL | wxRIGHT, FromDIP(6));
        auto* control = new wxSpinCtrl(parent, wxID_ANY, "", wxDefaultPosition, FromDIP(wxSize(108, -1)), wxSP_ARROW_KEYS, minimum, maximum, initial);
        row->Add(control); parent->GetSizer()->Add(row, 0, wxEXPAND | wxALL, FromDIP(10)); idleControls_.push_back(control);
        control->Bind(wxEVT_SPINCTRL, [this](wxSpinEvent&) { Changed(); }); control->Bind(wxEVT_TEXT, [this](wxCommandEvent&) { Changed(); }); return control;
    };
    auto seconds = [&](wxWindow* parent, const wxString& title, double maximum, double initial) {
        auto* row = new wxBoxSizer(wxHORIZONTAL); row->Add(new wxStaticText(parent, wxID_ANY, title), 1, wxALIGN_CENTER_VERTICAL | wxRIGHT, FromDIP(6));
        auto* control = new wxSpinCtrlDouble(parent, wxID_ANY, "", wxDefaultPosition, FromDIP(wxSize(108, -1)), wxSP_ARROW_KEYS, 1, maximum, initial, 1);
        control->SetDigits(3); row->Add(control); parent->GetSizer()->Add(row, 0, wxEXPAND | wxALL, FromDIP(10)); idleControls_.push_back(control);
        control->Bind(wxEVT_SPINCTRLDOUBLE, [this](wxSpinDoubleEvent&) { Changed(); }); control->Bind(wxEVT_TEXT, [this](wxCommandEvent&) { Changed(); }); return control;
    };
    auto* project = page("Project");
    label(project, "Source files (compilation order; drag files here)");
    sources_ = new wxListBox(project, wxID_ANY, wxDefaultPosition, FromDIP(wxSize(-1, 140)), 0, nullptr, wxLB_HSCROLL);
    project->GetSizer()->Add(sources_, 0, wxEXPAND | wxALL, FromDIP(10)); idleControls_.push_back(sources_);
    auto* order = new wxBoxSizer(wxHORIZONTAL);
    button(project, order, "Remove", wxID_ANY, [this](wxCommandEvent&) { auto index = sources_->GetSelection(); if (index != wxNOT_FOUND) { sources_->Delete(index); Changed(); } });
    auto move = [this](int delta) { int index = sources_->GetSelection(), target = index + delta; if (index >= 0 && target >= 0 && target < static_cast<int>(sources_->GetCount())) { auto value = sources_->GetString(index); sources_->Delete(index); sources_->Insert(value, target); sources_->SetSelection(target); Changed(); } };
    button(project, order, "Up", wxID_ANY, [move](wxCommandEvent&) { move(-1); });
    button(project, order, "Down", wxID_ANY, [move](wxCommandEvent&) { move(1); });
    project->GetSizer()->Add(order, 0, wxLEFT | wxRIGHT, FromDIP(10));
    label(project, "Top module (Inspect discovers choices)");
    top_ = new wxComboBox(project, wxID_ANY); top_->SetMaxLength(256);
    project->GetSizer()->Add(top_, 0, wxEXPAND | wxALL, FromDIP(10)); idleControls_.push_back(top_);
    top_->Bind(wxEVT_TEXT, [this](wxCommandEvent&) { Changed(); });
    output_ = text(project, "Generated output folder");
    auto* browse = new wxBoxSizer(wxHORIZONTAL); button(project, browse, "Choose output...", wxID_ANY, [this](wxCommandEvent&) { ChooseOutput(); });
    project->GetSizer()->Add(browse, 0, wxLEFT, FromDIP(10));
    includes_ = text(project, "Include folders (one per line)", true);
    defines_ = text(project, "Defines (one NAME=value per line)", true);
    parameters_ = text(project, "Top parameters (one NAME=value per line)", true);
    trace_ = text(project, "Optional sequential trace JSON");
    auto* traceButtons = new wxBoxSizer(wxHORIZONTAL);
    button(project, traceButtons, "Choose trace...", wxID_ANY, [this](wxCommandEvent&) {
        wxFileDialog dialog(this, "Sequential trace", "", "", "JSON (*.json)|*.json", wxFD_OPEN | wxFD_FILE_MUST_EXIST);
        if (dialog.ShowModal() == wxID_OK) trace_->SetValue(dialog.GetPath());
    }); project->GetSizer()->Add(traceButtons, 0, wxALL, FromDIP(10));
    auto* layout = page("Layout");
    layout_ = choice(layout, "Placement and routing", {"Compact (automatic)", "Reserved spacing"});
    spacing_ = spin(layout, "Extra spacing (grid units)", 40, 400, 80); spacing_->SetIncrement(20);
    profile_ = choice(layout, "Supported circuit profile", {"Automatic", "Scalar gates", "Readable datapath blocks", "Sequential registers"});
    flatten_ = check(layout, "Flatten hierarchy"); tunnels_ = check(layout, "Allow named tunnels");
    useModel_ = check(layout, "Enable optional local model refinement");
    model_ = choice(layout, "Local model", {"Qwen3 4B Q4 (recommended)", "Qwen3 4B Q5", "Qwen3 14B Q4 (advanced)"});
    model_->Bind(wxEVT_CHOICE, [this](wxCommandEvent& event) {
        if (!loading_ && model_->GetSelection() == 2) { context_->SetValue(2048); modelMemory_->SetValue(11264); memory_->SetValue(std::max(memory_->GetValue(), 12288)); SetStatusText("14B preset: 2048-token context, 11 GiB model ceiling, 12 GiB job ceiling. Admission checks current free memory."); }
        Changed(); event.Skip();
    });
    request_ = text(layout, "Layout preference for the model", true); request_->SetMaxLength(500);
    attempts_ = spin(layout, "Maximum proposals", 1, 8, 3); budget_ = seconds(layout, "Refinement budget (seconds)", 300, 120);
    auto* modelActions = new wxBoxSizer(wxHORIZONTAL);
    button(layout, modelActions, "Status", IdModels, [this](wxCommandEvent&) { ModelCommand(L"status"); });
    button(layout, modelActions, "Verify", IdVerifyModel, [this](wxCommandEvent&) { ModelCommand(L"verify"); });
    button(layout, modelActions, "Install", IdInstallModel, [this](wxCommandEvent&) { ModelCommand(L"install"); });
    layout->GetSizer()->Add(modelActions, 0, wxALL, FromDIP(10));
    auto* note = new wxStaticText(layout, wxID_ANY, "Refinement is optional. Every accepted move must preserve circuit behavior and improve the layout checks."); note->Wrap(FromDIP(310)); layout->GetSizer()->Add(note, 0, wxALL, FromDIP(10));
    auto* resources = page("Resources");
    label(resources, "Ceilings limit use; they do not reserve RAM.\nGPU placement is admitted from current free VRAM.");
    context_ = spin(resources, "Context tokens (0 = automatic)", 0, 8192, 0);
    modelMemory_ = spin(resources, "Model memory ceiling (MiB)", 64, 12288, 6144);
    idle_ = spin(resources, "Model idle unload (seconds)", 1, 300, 30);
    deadline_ = seconds(resources, "Whole job deadline (seconds)", 3600, 300);
    memory_ = spin(resources, "Whole job memory ceiling (MiB)", 256, 65536, 6144);
    javaHeap_ = spin(resources, "Java heap ceiling (MiB)", 64, 16384, 1024);
    vectors_ = spin(resources, "Random validation vectors", 0, 2048, 256);
    seed_ = spin(resources, "Validation seed", -2147483647 - 1, 2147483647, 1);
    auto* tools = new wxBoxSizer(wxHORIZONTAL); button(resources, tools, "Check tools and memory", IdDoctor, [this](wxCommandEvent&) { Begin(Action::Doctor, {L"doctor"}); });
    resources->GetSizer()->Add(tools, 0, wxALL, FromDIP(10));

    auto* right = new wxPanel(splitter); auto* rightSizer = new wxBoxSizer(wxVERTICAL);
    auto* previewTools = new wxBoxSizer(wxHORIZONTAL);
    view_ = new wxChoice(right, wxID_ANY, wxDefaultPosition, wxDefaultSize, {"Selected circuit", "Before refinement"}); view_->SetSelection(0);
    previewTools->Add(view_, 0, wxRIGHT, FromDIP(8));
    view_->Bind(wxEVT_CHOICE, [this](wxCommandEvent&) { wxString error; if (!preview_->ShowBaseline(view_->GetSelection() == 1, &error)) { view_->SetSelection(0); preview_->ShowBaseline(false); Error(Utf8(error)); } });
    auto previewButton = [&](const wxString& title, auto handler) { auto* b = new wxButton(right, wxID_ANY, title, wxDefaultPosition, FromDIP(wxSize(48, -1))); b->Bind(wxEVT_BUTTON, handler); previewTools->Add(b, 0, wxRIGHT, FromDIP(4)); };
    previewButton("Fit", [this](wxCommandEvent&) { preview_->Fit(); });
    previewButton("+", [this](wxCommandEvent&) { preview_->ZoomAt(1.25, wxPoint(preview_->GetClientSize().x / 2, preview_->GetClientSize().y / 2)); });
    previewButton("-", [this](wxCommandEvent&) { preview_->ZoomAt(0.8, wxPoint(preview_->GetClientSize().x / 2, preview_->GetClientSize().y / 2)); });
    overlays_ = new wxCheckBox(right, wxID_ANY, "Component overlays"); overlays_->SetValue(true); previewTools->Add(overlays_, 0, wxALIGN_CENTER_VERTICAL | wxLEFT, FromDIP(6));
    overlays_->Bind(wxEVT_CHECKBOX, [this](wxCommandEvent&) { preview_->SetOverlays(overlays_->GetValue()); });
    rightSizer->Add(previewTools, 0, wxEXPAND | wxALL, FromDIP(8));
    preview_ = new PreviewPanel(right); rightSizer->Add(preview_, 1, wxEXPAND | wxLEFT | wxRIGHT, FromDIP(8));
    selection_ = new wxStaticText(right, wxID_ANY, "Scroll: zoom | Drag: pan | Click: select", wxDefaultPosition, wxDefaultSize, wxST_ELLIPSIZE_END);
    rightSizer->Add(selection_, 0, wxALL | wxEXPAND, FromDIP(8));
    preview_->SetSelectionCallback([this](std::string id) { selection_->SetLabel(id.empty() ? "No component selected" : "Component: " + Wide(id)); });
    verification_ = new wxStaticText(right, wxID_ANY, "No result loaded."); rightSizer->Add(verification_, 0, wxLEFT | wxRIGHT | wxBOTTOM, FromDIP(8));
    details_ = new wxTextCtrl(right, wxID_ANY, "", wxDefaultPosition, FromDIP(wxSize(-1, 150)), wxTE_MULTILINE | wxTE_READONLY | wxTE_DONTWRAP);
    rightSizer->Add(details_, 0, wxEXPAND | wxALL, FromDIP(8)); right->SetSizer(rightSizer);
    splitter->SplitVertically(notebook, right, FromDIP(370)); outer->Add(splitter, 1, wxEXPAND);
    auto* status = new wxBoxSizer(wxHORIZONTAL); stage_ = new wxStaticText(this, wxID_ANY, "Ready"); status->Add(stage_, 1, wxALIGN_CENTER_VERTICAL | wxRIGHT, FromDIP(8));
    progress_ = new wxGauge(this, wxID_ANY, 100, wxDefaultPosition, FromDIP(wxSize(200, 16))); status->Add(progress_, 0, wxALIGN_CENTER_VERTICAL);
    outer->Add(status, 0, wxEXPAND | wxALL, FromDIP(10)); SetSizer(outer);
}

void MainFrame::Menus() {
    auto* file = new wxMenu; file->Append(wxID_NEW, "&New project\tCtrl+N"); file->Append(wxID_OPEN, "&Open project...\tCtrl+O");
    file->Append(wxID_SAVE, "&Save project\tCtrl+S"); file->Append(IdSaveAs, "Save project &as...\tCtrl+Shift+S"); file->AppendSeparator();
    file->Append(IdOpenResult, "Reopen circuit package..."); file->Append(IdExport, "Export circuit package..."); file->Append(IdDigital, "Open in Digital"); file->AppendSeparator(); file->Append(wxID_EXIT, "E&xit\tAlt+F4");
    auto* run = new wxMenu; run->Append(IdInspect, "Inspect tops\tCtrl+I"); run->Append(IdConvert, "Convert\tF5"); run->Append(IdCancel, "Cancel\tEsc"); run->AppendSeparator(); run->Append(IdDoctor, "Check tools and memory");
    auto* help = new wxMenu; help->Append(wxID_HELP, "Using the application"); help->Append(wxID_ABOUT, "About");
    auto* bar = new wxMenuBar; bar->Append(file, "&File"); bar->Append(run, "&Run"); bar->Append(help, "&Help"); SetMenuBar(bar);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { if (!Busy() && CanDiscard()) NewProject(); }, wxID_NEW);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) {
        if (Busy() || !CanDiscard()) return;
        wxFileDialog dialog(this, "Open Verilog to Digital project", "", "", "Project JSON (*.json)|*.json", wxFD_OPEN | wxFD_FILE_MUST_EXIST);
        if (dialog.ShowModal() == wxID_OK) { try { OpenProject(Path(dialog.GetPath())); } catch (const std::exception& e) { Error(e.what()); } }
    }, wxID_OPEN);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { SaveProject(false); }, wxID_SAVE);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { SaveProject(true); }, IdSaveAs);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { Close(); }, wxID_EXIT);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { RunProject(true); }, IdInspect);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { RunProject(false); }, IdConvert);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { if (Busy() && bridge_) { bridge_->Cancel(); stage_->SetLabel("Cancelling..."); } }, IdCancel);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { OpenResult(); }, IdOpenResult);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { ExportResult(); }, IdExport);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { if (!Busy() && !resultDirectory_.empty()) Begin(Action::Digital, {L"desktop", L"digital", resultDirectory_.wstring()}); }, IdDigital);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { Begin(Action::Doctor, {L"doctor"}); }, IdDoctor);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) {
        wxMessageBox("1. Add .v files in compilation order, or open a project JSON.\n2. Inspect tops and select the module to convert.\n3. Set the output folder and choose Convert.\n\nDrag to pan; scroll to zoom; + / - / 0 and arrow keys work in the preview. Export writes a new complete circuit package folder. Reopen checks the package and native model; it displays the original run's recorded HDL coverage.\n\nOptional model refinement is off by default. The 4B Q4 model is the measured default when enabled. Memory ceilings do not reserve RAM; current free memory determines GPU admission.", "Using Verilog to Digital", wxOK | wxICON_INFORMATION, this);
    }, wxID_HELP);
    Bind(wxEVT_MENU, [this](wxCommandEvent&) { wxMessageBox("Verilog to Digital 0.1\nNative Digital rendering and independent electrical/HDL checks.\nC++ wxWidgets interface with isolated Python workers.", "About", wxOK | wxICON_INFORMATION, this); }, wxID_ABOUT);
}

void MainFrame::UpdateEnabled() {
    for (auto* control : idleControls_) control->Enable(!Busy());
    cancel_->Enable(Busy()); convert_->Enable(!Busy() && sources_->GetCount());
    export_->Enable(!Busy() && !resultDirectory_.empty()); digital_->Enable(!Busy() && !resultDirectory_.empty());
    view_->Enable(preview_->HasBaseline()); overlays_->Enable(preview_->HasMetadata());
    if (GetMenuBar()) {
        for (int id : std::vector<int>{wxID_NEW, wxID_OPEN, wxID_SAVE, IdSaveAs, IdOpenResult, IdInspect, IdDoctor}) GetMenuBar()->Enable(id, !Busy());
        GetMenuBar()->Enable(IdConvert, !Busy() && sources_->GetCount()); GetMenuBar()->Enable(IdCancel, Busy());
        GetMenuBar()->Enable(IdExport, !Busy() && !resultDirectory_.empty()); GetMenuBar()->Enable(IdDigital, !Busy() && !resultDirectory_.empty());
    }
}
void MainFrame::Changed() { if (!loading_) { dirty_ = true; SetTitle("Verilog to Digital - " + (projectFile_.empty() ? wxString("Untitled") : Display(projectFile_.filename())) + " *"); UpdateEnabled(); } }
void MainFrame::Error(const std::string& message) { stage_->SetLabel("Operation failed"); details_->SetValue(Wide(message)); SetStatusText("The previous valid result remains available."); }
bool MainFrame::CanDiscard() {
    if (!dirty_) return true;
    int answer = wxMessageBox("Save project changes before continuing?", "Unsaved project", wxYES_NO | wxCANCEL | wxICON_QUESTION, this);
    return answer == wxNO || (answer == wxYES && SaveProject(false));
}
void MainFrame::NewProject() {
    ApplyProject({{"schema_version", 1}, {"sources", json::array()}}); projectFile_.clear(); dirty_ = false;
    SetTitle("Verilog to Digital - Untitled");
}
void MainFrame::AddSources(const wxArrayString& paths) {
    if (Busy()) return;
    for (const auto& name : paths) {
        if (sources_->GetCount() >= 64) { Error("A project may contain at most 64 source files"); break; }
        auto file = std::filesystem::absolute(Path(name));
        if (!std::filesystem::is_regular_file(file)) { Error("Source file does not exist: " + file.u8string()); continue; }
        auto display = Display(file); if (sources_->FindString(display, false) == wxNOT_FOUND) sources_->Append(display);
        if (output_->GetValue().empty()) output_->SetValue(Display(file.parent_path() / "generated"));
    }
    Changed();
}
json MainFrame::Project() const {
    auto value = savedExtras_; value["schema_version"] = 1; value["sources"] = json::array();
    for (unsigned i = 0; i < sources_->GetCount(); ++i) value["sources"].push_back(Path(sources_->GetString(i)).u8string());
    if (value["sources"].empty()) throw std::runtime_error("Add at least one Verilog source file");
    auto resolve = [&](const wxString& name) {
        auto path = Path(name); auto base = projectFile_.empty() ? root_ : projectFile_.parent_path();
        return std::filesystem::absolute(path.is_absolute() ? path : base / path).lexically_normal().u8string();
    };
    value["include_dirs"] = json::array(); for (const auto& line : Lines(includes_->GetValue())) value["include_dirs"].push_back(resolve(line));
    value["defines"] = Definitions(defines_->GetValue()); value["parameters"] = Definitions(parameters_->GetValue());
    value.erase("top"); if (!top_->GetValue().empty()) value["top"] = Utf8(top_->GetValue());
    value.erase("output"); if (!output_->GetValue().empty()) value["output"] = resolve(output_->GetValue());
    value.erase("trace"); if (!trace_->GetValue().empty()) value["trace"] = resolve(trace_->GetValue());
    if (spacing_->GetValue() % 20) throw std::runtime_error("Spacing must be a multiple of 20 grid units");
    if (context_->GetValue() && context_->GetValue() < 1024) throw std::runtime_error("Context must be automatic (0), or 1024 to 8192 tokens");
    auto& conversion = value["conversion"]; if (conversion.is_null()) conversion = json::object();
    conversion.update({{"spacing", spacing_->GetValue()}, {"layout_strategy", layout_->GetSelection() ? "reserved" : "auto"}, {"profile", profiles[profile_->GetSelection()]},
        {"flatten", flatten_->GetValue()}, {"tunnels", tunnels_->GetValue()}, {"seed", seed_->GetValue()}, {"random_count", vectors_->GetValue()}});
    conversion["refinement"] = {{"mode", useModel_->GetValue() ? "local" : "off"}, {"model_id", models[model_->GetSelection()]},
        {"request", Utf8(request_->GetValue())}, {"max_attempts", attempts_->GetValue()}, {"budget_seconds", budget_->GetValue()},
        {"context_tokens", context_->GetValue() ? json(context_->GetValue()) : json(nullptr)}, {"model_memory_mib", modelMemory_->GetValue()}, {"idle_seconds", idle_->GetValue()}};
    auto& resources = value["resources"]; if (resources.is_null()) resources = json::object();
    resources.update({{"deadline_seconds", deadline_->GetValue()}, {"memory_mib", memory_->GetValue()}, {"java_heap_mib", javaHeap_->GetValue()}});
    return value;
}
void MainFrame::ApplyProject(const json& value) {
    loading_ = true;
    try {
        auto conversion = value.value("conversion", json::object()); auto resources = value.value("resources", json::object());
        auto refinement = conversion.value("refinement", json::object());
        if (refinement.value("mode", "off") == "replay") throw std::runtime_error("Recorded action replay is available through the CLI; open a project using off or local refinement in the desktop");
        // Read and validate all typed settings before replacing the visible project.
        const std::string model = refinement.value("model_id", "qwen3-4b-q4_k_m"), profile = conversion.value("profile", "auto");
        auto modelIndex = std::find(std::begin(models), std::end(models), model) - std::begin(models);
        auto profileIndex = std::find(std::begin(profiles), std::end(profiles), profile) - std::begin(profiles);
        if (modelIndex == 3 || profileIndex == 4) throw std::runtime_error("Unknown model or circuit profile");
        struct Setting { wxSpinCtrl* control; int value; };
        if (conversion.contains("seed") && (conversion["seed"].get<double>() < -2147483648.0 || conversion["seed"].get<double>() > 2147483647.0)) throw std::runtime_error("The desktop seed control supports signed 32-bit integers");
        std::vector<Setting> settings{{spacing_, conversion.value("spacing", 80)}, {attempts_, refinement.value("max_attempts", 3)},
            {context_, refinement.contains("context_tokens") && !refinement["context_tokens"].is_null() ? refinement["context_tokens"].get<int>() : 0},
            {modelMemory_, refinement.value("model_memory_mib", 6144)}, {idle_, refinement.value("idle_seconds", 30)},
            {memory_, resources.value("memory_mib", 6144)}, {javaHeap_, resources.value("java_heap_mib", 1024)}, {vectors_, conversion.value("random_count", 256)}, {seed_, conversion.value("seed", 1)}};
        for (const auto& setting : settings) if (setting.value < setting.control->GetMin() || setting.value > setting.control->GetMax()) throw std::runtime_error("Saved project has an out-of-range setting");
        const double budget = refinement.value("budget_seconds", 120.0), deadline = resources.value("deadline_seconds", 300.0);
        if (!std::isfinite(budget) || budget < 1 || budget > 300 || !std::isfinite(deadline) || deadline < 1 || deadline > 3600) throw std::runtime_error("Saved project has an out-of-range time budget");
        const auto sourceText = value.at("sources").get<std::vector<std::string>>();
        const auto includeText = ListText(value.value("include_dirs", json::array()));
        const auto defineText = DefinitionText(value.value("defines", json::object())), parameterText = DefinitionText(value.value("parameters", json::object()));
        const auto top = value.value("top", ""), output = value.value("output", ""), trace = value.value("trace", "");
        const bool flatten = conversion.value("flatten", false), tunnels = conversion.value("tunnels", false);
        const auto mode = refinement.value("mode", "off"), strategy = conversion.value("layout_strategy", "auto");
        const auto request = refinement.value("request", "Reduce area, wire length, bends and crossings while preserving spacing.");
        if ((mode != "off" && mode != "local") || (strategy != "auto" && strategy != "reserved")) throw std::runtime_error("Unknown refinement or placement mode");
        sources_->Clear(); for (const auto& name : sourceText) sources_->Append(Wide(name));
        top_->Clear(); top_->SetValue(Wide(top)); output_->ChangeValue(Wide(output)); trace_->ChangeValue(Wide(trace));
        includes_->ChangeValue(includeText); defines_->ChangeValue(defineText); parameters_->ChangeValue(parameterText);
        for (const auto& setting : settings) setting.control->SetValue(setting.value);
        budget_->SetValue(budget); deadline_->SetValue(deadline);
        flatten_->SetValue(flatten); tunnels_->SetValue(tunnels); useModel_->SetValue(mode == "local");
        model_->SetSelection(static_cast<int>(modelIndex)); profile_->SetSelection(static_cast<int>(profileIndex)); layout_->SetSelection(strategy == "reserved" ? 1 : 0);
        request_->ChangeValue(Wide(request)); savedExtras_ = value;
        loading_ = false; dirty_ = false; UpdateEnabled();
    } catch (...) { loading_ = false; throw; }
}
void MainFrame::OpenProject(const std::filesystem::path& file) {
    if (Busy()) throw std::runtime_error("Wait for the current command to finish");
    auto absolute = std::filesystem::absolute(file); auto value = LoadProject(absolute); ApplyProject(value); projectFile_ = absolute;
    SetTitle("Verilog to Digital - " + Display(file.filename())); SetStatusText("Project loaded. Inspect tops to check the current source files.");
}
bool MainFrame::SaveProject(bool saveAs) {
    try {
        auto value = Project(); auto file = projectFile_;
        if (saveAs || file.empty()) {
            wxFileDialog dialog(this, "Save project", file.empty() ? Display(root_) : Display(file.parent_path()), file.empty() ? wxString("project.v2d.json") : Display(file.filename()), "Project JSON (*.json)|*.json", wxFD_SAVE | wxFD_OVERWRITE_PROMPT);
            if (dialog.ShowModal() != wxID_OK) return false; file = Path(dialog.GetPath());
        }
        WriteJson(file, RelativeProject(value, file), true); projectFile_ = file; savedExtras_ = value; dirty_ = false;
        SetTitle("Verilog to Digital - " + Display(file.filename())); SetStatusText("Project saved."); return true;
    } catch (const std::exception& e) { Error(e.what()); return false; }
}

void MainFrame::Begin(Action action, std::vector<std::wstring> args, int seconds) {
    if (Busy()) return;
    try {
        if (bridge_) bridge_->Shutdown();
        BridgeOptions options; options.command_timeout = std::chrono::seconds(seconds);
        bridge_ = std::make_unique<WorkerBridge>(*this, root_, options);
        action_ = action; generation_ = bridge_->Start(args);
        stage_->SetLabel("Starting..."); progress_->Pulse(); pulse_.Start(100); UpdateEnabled();
    } catch (const std::exception& e) { action_ = Action::None; pulse_.Stop(); UpdateEnabled(); Error(e.what()); }
}
void MainFrame::RunProject(bool inspect) {
    if (Busy()) return;
    try {
        auto project = Project();
        if (!inspect && !project.contains("output")) throw std::runtime_error("Choose a generated output folder");
        auto directory = root_ / ".runs/gui"; std::filesystem::create_directories(directory);
        snapshot_ = directory / ("project-" + std::to_string(wxGetProcessId()) + "-" + std::to_string(wxGetUTCTimeMillis().GetValue()) + ".json");
        WriteJson(snapshot_, project, false);
        std::vector<std::wstring> args{inspect ? L"inspect" : L"convert", L"--project", snapshot_.wstring()};
        if (!inspect) args.push_back(L"--replace");
        Begin(inspect ? Action::Inspect : Action::Convert, args, static_cast<int>(std::ceil(deadline_->GetValue())) + 30);
        if (!Busy()) { std::error_code error; std::filesystem::remove(snapshot_, error); snapshot_.clear(); }
    } catch (const std::exception& e) { Error(e.what()); }
}
void MainFrame::OnWorker(wxThreadEvent&) {
    if (!bridge_ || closing_) return;
    auto updates = bridge_->DrainEvents();
    for (const auto& update : updates) {
        if (update.generation != generation_ || !Busy()) continue;
        if (update.kind == WorkerUpdate::Kind::Progress) {
            try {
                const auto& event = update.envelope.at("event");
                auto stage = event.value("stage", event.value("state", "running")); std::replace(stage.begin(), stage.end(), '_', ' '); stage_->SetLabel(Wide(stage));
            } catch (const std::exception&) { bridge_->Cancel(); Error("The worker returned malformed progress data; cancellation requested."); }
        } else { Completed(update); break; }
    }
}
void MainFrame::Completed(const WorkerUpdate& update) {
    auto action = action_; action_ = Action::None; pulse_.Stop(); progress_->SetValue(0);
    if (!snapshot_.empty()) { std::error_code error; std::filesystem::remove(snapshot_, error); snapshot_.clear(); }
    UpdateEnabled();
    try {
        if (!update.diagnostic_code.empty()) throw std::runtime_error(update.diagnostic_code + ": " + update.diagnostic_message + (update.stderr_tail.empty() ? "" : "\n" + update.stderr_tail));
        const auto& result = update.envelope.at("result");
        const auto state = result.value("state", "ready");
        if (update.envelope.value("type", "") == "error" || state == "failed" || state == "cancelled" || state == "interrupted" || result.value("valid", true) == false) {
            auto diagnostic = result.value("diagnostic", json{{"code", "COMMAND_FAILED"}, {"message", "The operation did not produce a valid result"}});
            Error(Utf8(Message(diagnostic))); if (state == "cancelled") stage_->SetLabel("Cancelled"); return;
        }
        if (action == Action::Convert) {
            Begin(Action::Result, {L"desktop", L"result", Wide(result.at("manifest").get<std::string>()).ToStdWstring()}); return;
        }
        if (action == Action::Result) ShowResult(result);
        else if (action == Action::Inspect) {
            const auto& inspection = result.at("inspection"); auto previous = top_->GetValue(); loading_ = true;
            top_->Clear(); for (const auto& candidate : inspection.at("top_candidates")) top_->Append(Wide(candidate.get<std::string>()));
            if (!previous.empty()) top_->SetValue(previous);
            else if (top_->GetCount() == 1) { top_->SetSelection(0); dirty_ = true; }
            loading_ = false;
            wxString report = "Source inspection passed.\nAvailable top modules:\n";
            for (const auto& candidate : inspection.at("top_candidates")) report += "  " + Wide(candidate.get<std::string>()) + "\n";
            for (const auto& item : inspection.value("diagnostics", json::array())) report += Message(item) + "\n";
            details_->SetValue(report); SetStatusText(top_->GetCount() > 1 ? "Choose the top module, then Convert." : "Source inspection passed. Ready to convert.");
        } else if (action == Action::Digital) {
            std::vector<std::wstring> args; for (const auto& arg : result.at("argv")) args.push_back(Wide(arg.get<std::string>()).ToStdWstring());
            const auto pid = LaunchDetached(args, std::filesystem::u8path(result.at("cwd").get<std::string>())); SetStatusText(wxString::Format("Digital opened (process %lu).", static_cast<unsigned long>(pid)));
        } else if (action == Action::Export) {
            details_->SetValue("Exported complete circuit package:\n" + Wide(result.at("directory").get<std::string>()) + "\nFiles: " + Wide(result.at("files").dump())); SetStatusText("Package exported. The selected result is unchanged.");
        } else if (action == Action::Models) {
            wxString report;
            if (result.contains("models") && result["models"].is_array()) {
                report = "Model inventory (file presence; Verify checks all hashes):\n\n";
                for (const auto& model : result["models"]) report += Wide(model.at("model_id").get<std::string>()) + ": " + (model.value("files_present", false) ? "present" : "not installed") + "\n";
            } else report = result.value("verified", false) ? "Model and runtime hashes verified." : "Model installation completed and verified.";
            details_->SetValue(report);
        } else if (action == Action::Doctor) {
            wxString report = "Runtime diagnostics\n";
            if (result.contains("tools")) for (const auto& item : result["tools"].items()) {
                report += Wide(item.key()) + ": " + (item.value().value("available", false) ? "available" : "missing or failed") + "\n";
                if (item.value().contains("version")) report += "  " + Wide(item.value()["version"].get<std::string>()) + "\n";
                if (item.value().contains("error")) report += "  " + Wide(item.value()["error"].get<std::string>()) + "\n";
            }
            if (result.contains("memory") && result["memory"].is_object()) for (const auto& item : result["memory"].items()) {
                if (item.value().is_number() && item.key().find("bytes") != std::string::npos) {
                    auto name = item.key(); std::replace(name.begin(), name.end(), '_', ' ');
                    report += Wide(name) + wxString::Format(": %.1f MiB\n", item.value().get<double>() / (1024.0 * 1024.0));
                }
            }
            details_->SetValue(report);
        }
        stage_->SetLabel("Ready"); UpdateEnabled();
    } catch (const std::exception& e) { loading_ = false; Error(e.what()); UpdateEnabled(); }
}
void MainFrame::ShowResult(const json& result) {
    auto directory = std::filesystem::u8path(result.at("directory").get<std::string>());
    auto file = directory / "manifest.json";
    const auto expected = result.at("manifest_sha256").get<std::string>();
    if (Sha256File(file) != expected) throw std::runtime_error("The checked result manifest changed before display");
    auto manifest = ReadJson(file, 8 * 1024 * 1024);
    for (const auto& field : result.at("manifest").items()) if (manifest.at(field.key()) != field.value()) throw std::runtime_error("The result display binding changed");
    json metadata;
    if (!result.at("metadata_path").is_null()) metadata = ReadJson(std::filesystem::u8path(result["metadata_path"].get<std::string>()), 8 * 1024 * 1024);
    const auto& summary = result.at("summary"); wxString text = "Top: " + Wide(result["manifest"]["top"].get<std::string>()) + "\n";
    if (!summary.at("native_model_valid").get<bool>() || !summary.at("artifact_binding_valid").get<bool>()) throw std::runtime_error("The result did not pass its native circuit checks");
    const auto& metrics = summary.at("layout_metrics");
    for (const auto& pair : std::vector<std::pair<const char*, const char*>>{{"bounding_area", "Area (grid units squared)"}, {"wire_length", "Wire length (grid units)"}, {"bends", "Bends"}, {"crossings", "Crossings"}})
        if (metrics.contains(pair.first)) text += Wide(pair.second) + ": " + Wide(metrics[pair.first].dump()) + "\n";
    const auto& behavior = summary.at("recorded_behavior");
    if (behavior.is_object()) {
        text += "\nRecorded comparison with original Verilog: " + wxString(behavior.value("valid", false) ? "passed" : "unavailable") + "\n";
        if (behavior.contains("coverage")) { auto coverage = behavior["coverage"].get<std::string>(); std::replace(coverage.begin(), coverage.end(), '_', ' '); text += "Coverage: " + Wide(coverage) + "\n"; }
        for (const auto& pair : std::vector<std::pair<const char*, const char*>>{{"input_vectors", "Input vectors"}, {"cycles", "Clock cycles"}, {"phases", "Checked phases"}, {"seed", "Validation seed"}})
            if (behavior.contains(pair.first)) text += Wide(pair.second) + ": " + Wide(behavior[pair.first].dump()) + "\n";
        text += "Reopening checks artifacts and the native circuit; it does not replay HDL simulation.\n";
    }
    const auto& refinement = summary.at("refinement");
    text += "\nLocal refinement: " + Wide(refinement.value("mode", "off")) + "\n";
    if (refinement.contains("attempted")) text += "Proposals attempted: " + Wide(refinement["attempted"].dump()) + "\n";
    if (refinement.contains("accepted")) text += "Accepted proposals: " + Wide(refinement["accepted"].dump()) + "\n";
    for (const auto& diagnostic : refinement.value("diagnostics", json::array())) text += Message(diagnostic) + "\n";
    wxString error;
    auto candidate = std::make_unique<PreviewPanel>(preview_->GetParent()); candidate->Hide();
    if (!candidate->LoadResult(directory, manifest, metadata, &error) || Sha256File(file) != expected)
        throw std::runtime_error(error.empty() ? "The result changed during preview loading" : Utf8(error));
    candidate->SetSelectionCallback([this](std::string id) { selection_->SetLabel(id.empty() ? "No component selected" : "Component: " + Wide(id)); });
    auto* parent = preview_->GetParent(); parent->GetSizer()->Replace(preview_, candidate.get());
    auto* previous = preview_; preview_ = candidate.release();
    previous->Destroy(); preview_->Show(); parent->Layout(); preview_->Fit();
    checkedResult_ = result; resultDirectory_ = directory; view_->SetSelection(0); preview_->SetOverlays(overlays_->GetValue());
    selection_->SetLabel("Scroll: zoom | Drag: pan | Click: select");
    verification_->SetLabel("Native circuit and saved files: passed.\nHDL coverage was recorded during conversion.");
    parent->Layout();
    details_->SetValue(text); SetStatusText("Verified circuit package: " + Display(directory)); UpdateEnabled();
}
void MainFrame::ChooseOutput() {
    wxDirDialog dialog(this, "Choose generated output folder", output_->GetValue(), wxDD_DEFAULT_STYLE);
    if (dialog.ShowModal() == wxID_OK) output_->SetValue(dialog.GetPath());
}
void MainFrame::OpenResult() {
    if (Busy()) return;
    wxFileDialog dialog(this, "Reopen circuit package", "", "", "Result manifest or selection (*.json)|*.json", wxFD_OPEN | wxFD_FILE_MUST_EXIST);
    if (dialog.ShowModal() == wxID_OK) Begin(Action::Result, {L"desktop", L"result", dialog.GetPath().ToStdWstring()});
}
void MainFrame::ExportResult() {
    if (Busy() || resultDirectory_.empty()) return;
    wxFileDialog dialog(this, "Name a new circuit package folder", "", "circuit-package", "Package folder name|*", wxFD_SAVE);
    if (dialog.ShowModal() == wxID_OK) Begin(Action::Export, {L"desktop", L"export", resultDirectory_.wstring(), L"--output", dialog.GetPath().ToStdWstring()});
}
void MainFrame::ModelCommand(const std::wstring& command) {
    Begin(Action::Models, {L"models", command, L"--model", Wide(models[model_->GetSelection()]).ToStdWstring()}, command == L"install" ? 7200 : 330);
}
void MainFrame::OnClose(wxCloseEvent& event) {
    if (event.CanVeto() && !CanDiscard()) { event.Veto(); return; }
    closing_ = true; pulse_.Stop(); if (bridge_) bridge_->Shutdown(std::chrono::milliseconds(1500));
    if (!snapshot_.empty()) { std::error_code error; std::filesystem::remove(snapshot_, error); }
    Destroy();
}
json MainFrame::TestState() const {
    return {{"busy", Busy()}, {"sources", sources_->GetCount()}, {"top", Utf8(top_->GetValue())}, {"stage", Utf8(stage_->GetLabel())},
        {"details", Utf8(details_->GetValue())}, {"result_directory", resultDirectory_.u8string()}, {"preview_size", {preview_->GetImageSize().x, preview_->GetImageSize().y}},
        {"metadata", preview_->HasMetadata()}, {"baseline", preview_->HasBaseline()}, {"dirty", dirty_}};
}
