#pragma once

#include <wx/panel.h>
#include <wx/geometry.h>
#include <nlohmann/json_fwd.hpp>

#include <filesystem>
#include <functional>
#include <memory>
#include <string>

// Read-only native Digital preview. All coordinates supplied to ZoomAt and
// HitTest are wx client coordinates; metadata refers to physical PNG pixels.
class PreviewPanel final : public wxPanel {
public:
    explicit PreviewPanel(wxWindow* parent, wxWindowID id = wxID_ANY);
    ~PreviewPanel() override;

    // Failure clears any previous preview. Metadata may be null for legacy
    // manifests: the hash-bound PNG remains viewable without ID overlays.
    bool LoadResult(const std::filesystem::path& directory,
                    const nlohmann::json& manifest,
                    const nlohmann::json& metadata,
                    wxString* error = nullptr);
    bool ShowBaseline(bool show, wxString* error = nullptr);
    void Fit();
    void ZoomAt(double factor, const wxPoint& clientPoint);
    void PanBy(const wxPoint& delta);
    void SetOverlays(bool enabled);
    void SetSelectionCallback(std::function<void(std::string)> callback);
    void Clear();

    double GetZoom() const;
    std::string GetSelection() const;
    bool HasBaseline() const;
    bool IsShowingBaseline() const;
    bool IsBaselineLoaded() const;
    bool HasMetadata() const;
    wxSize GetImageSize() const;
    wxPoint2DDouble ImageToClient(const wxPoint2DDouble& point) const;
    wxPoint2DDouble ClientToImage(const wxPoint2DDouble& point) const;
    std::string HitTest(const wxPoint& clientPoint) const;
    void SelectAt(const wxPoint& clientPoint);

private:
    struct State;
    std::unique_ptr<State> state_;
    void OnPaint(wxPaintEvent& event);
    void OnSize(wxSizeEvent& event);
    void OnMouseDown(wxMouseEvent& event);
    void OnMouseUp(wxMouseEvent& event);
    void OnMouseMove(wxMouseEvent& event);
    void OnMouseWheel(wxMouseEvent& event);
    void OnCaptureLost(wxMouseCaptureLostEvent& event);
    void OnKey(wxKeyEvent& event);
    void NotifySelection();
};
