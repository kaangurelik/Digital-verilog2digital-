#include "main_frame.h"
#include "project_document.h"
#include <wx/stdpaths.h>

class Application final : public wxApp {
public:
    bool OnInit() override {
        wxInitAllImageHandlers();
        std::filesystem::path root, project;
        bool smoke = false;
        wxString configured;
        if (wxGetEnv("V2D_ROOT", &configured)) root = v2d::Path(configured);
        for (int index = 1; index < argc; ++index) {
            wxString arg(argv[index]);
            if (arg == "--smoke-test") smoke = true;
            else if (arg == "--root" && index + 1 < argc) root = v2d::Path(wxString(argv[++index]));
            else if (arg == "--project" && index + 1 < argc) project = v2d::Path(wxString(argv[++index]));
            else { wxMessageBox("Unknown or incomplete option: " + arg, "Verilog to Digital", wxOK | wxICON_ERROR); return false; }
        }
        if (root.empty()) {
            auto path = v2d::Path(wxStandardPaths::Get().GetExecutablePath()).parent_path();
            for (int level = 0; level < 5; ++level) {
                if (std::filesystem::is_regular_file(path / "scripts/v2d.py")) { root = path; break; }
                if (path == path.parent_path()) break; path = path.parent_path();
            }
        }
        if (root.empty()) { wxMessageBox("Cannot locate the application runtime. Use the supplied launcher or --root.", "Verilog to Digital", wxOK | wxICON_ERROR); return false; }
        try {
            auto* frame = new MainFrame(root); SetTopWindow(frame);
            if (!project.empty()) frame->OpenProject(std::filesystem::absolute(project));
            if (smoke) { CallAfter([frame]() { frame->Close(true); }); return true; }
            frame->Show(); return true;
        } catch (const std::exception& error) { wxMessageBox(v2d::Wide(error.what()), "Verilog to Digital", wxOK | wxICON_ERROR); return false; }
    }
};

wxIMPLEMENT_APP(Application);
