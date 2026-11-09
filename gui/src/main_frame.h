#pragma once
#include "worker_bridge.h"
#include <wx/wx.h>
#include <wx/combobox.h>
#include <wx/notebook.h>
#include <wx/spinctrl.h>
#include <filesystem>
#include <memory>

class PreviewPanel;
class MainFrame final : public wxFrame {
public:
    explicit MainFrame(std::filesystem::path root);
    ~MainFrame() override;
    void OpenProject(const std::filesystem::path& file);
    void AddSources(const wxArrayString& paths);
    bool Busy() const;
    nlohmann::json TestState() const;
private:
    enum class Action { None, Inspect, Convert, Result, Export, Digital, Doctor, Models };
    std::filesystem::path root_, projectFile_, snapshot_, resultDirectory_;
    nlohmann::json savedExtras_ = nlohmann::json::object(), checkedResult_;
    std::unique_ptr<v2d::WorkerBridge> bridge_;
    std::uint64_t generation_ = 0;
    Action action_ = Action::None;
    bool dirty_ = false, loading_ = false, closing_ = false;
    std::vector<wxWindow*> idleControls_;
    wxListBox* sources_;
    wxComboBox* top_;
    wxTextCtrl *includes_, *defines_, *parameters_, *output_, *trace_, *request_, *details_;
    wxChoice *layout_, *profile_, *model_, *view_;
    wxCheckBox *flatten_, *tunnels_, *useModel_, *overlays_;
    wxSpinCtrl *spacing_, *attempts_, *context_, *modelMemory_, *idle_, *memory_, *javaHeap_, *vectors_, *seed_;
    wxSpinCtrlDouble *budget_, *deadline_;
    wxButton *convert_, *cancel_, *export_, *digital_;
    wxStaticText *stage_, *selection_, *verification_;
    wxGauge* progress_;
    PreviewPanel* preview_;
    wxTimer pulse_;
    void BuildInterface();
    void Menus();
    void UpdateEnabled();
    void Changed();
    void Error(const std::string& message);
    bool CanDiscard();
    void NewProject();
    bool SaveProject(bool saveAs);
    nlohmann::json Project() const;
    void ApplyProject(const nlohmann::json& project);
    void Begin(Action action, std::vector<std::wstring> args, int seconds = 330);
    void RunProject(bool inspect);
    void OnWorker(wxThreadEvent& event);
    void Completed(const v2d::WorkerUpdate& update);
    void ShowResult(const nlohmann::json& result);
    void ChooseOutput();
    void OpenResult();
    void ExportResult();
    void ModelCommand(const std::wstring& command);
    void OnClose(wxCloseEvent& event);
};
