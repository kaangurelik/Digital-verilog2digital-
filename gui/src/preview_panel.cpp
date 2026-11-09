#include "preview_panel.h"

#include <wx/dcbuffer.h>
#include <wx/graphics.h>
#include <wx/image.h>
#include <wx/imagpng.h>
#include <wx/mstream.h>
#include <wx/log.h>
#include <wx/msw/wrapwin.h>
#include <bcrypt.h>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <vector>

namespace {
using Json = nlohmann::json;
using Bytes = std::vector<unsigned char>;
constexpr std::size_t kJsonLimit = 16 * 1024 * 1024;
constexpr std::size_t kImageLimit = 128 * 1024 * 1024;
constexpr int kDimensionLimit = 16384;
constexpr std::uint64_t kPixelLimit = 16000000;
constexpr double kMinimumZoom = .00001;
constexpr double kMaximumZoom = 32;

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

bool IsHash(const std::string& text) {
    return text.size() == 64 && std::all_of(text.begin(), text.end(), [](char ch) {
        return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f');
    });
}

std::string Hash(const Bytes& bytes) {
    struct Handles {
        BCRYPT_ALG_HANDLE algorithm = nullptr;
        BCRYPT_HASH_HANDLE hash = nullptr;
        ~Handles() {
            if (hash) BCryptDestroyHash(hash);
            if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
        }
    } handles;
    Require(BCryptOpenAlgorithmProvider(&handles.algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0,
            "Cannot initialize preview SHA-256 verification");
    Require(BCryptCreateHash(handles.algorithm, &handles.hash, nullptr, 0, nullptr, 0, 0) >= 0,
            "Cannot create preview SHA-256 verification");
    if (!bytes.empty())
        Require(BCryptHashData(handles.hash, const_cast<PUCHAR>(bytes.data()), static_cast<ULONG>(bytes.size()), 0) >= 0,
                "Cannot hash preview artifact");
    std::array<unsigned char, 32> result{};
    Require(BCryptFinishHash(handles.hash, result.data(), static_cast<ULONG>(result.size()), 0) >= 0,
            "Cannot finish preview SHA-256 verification");
    static const char hex[] = "0123456789abcdef";
    std::string text;
    for (unsigned char value : result) {
        text.push_back(hex[value >> 4]);
        text.push_back(hex[value & 15]);
    }
    return text;
}

std::filesystem::path Contained(const std::filesystem::path& directory, const std::string& relative) {
    Require(!relative.empty() && relative.find('\0') == std::string::npos, "Invalid preview artifact path");
    auto path = std::filesystem::u8path(relative);
    Require(!path.is_absolute() && !path.has_root_name() && !path.has_root_directory(),
            "Preview artifacts must remain inside the result directory");
    for (const auto& part : path)
        Require(part != ".." && part != ".", "Preview artifact traversal is not allowed");
    auto resolved = std::filesystem::canonical(directory / path);
    auto relation = resolved.lexically_relative(directory);
    Require(!relation.empty() && !relation.is_absolute(), "Preview artifact is outside the result directory");
    for (const auto& part : relation)
        Require(part != "..", "Preview artifact is outside the result directory");
    Require(std::filesystem::is_regular_file(resolved), "Preview artifact is not a regular file");
    return resolved;
}

Bytes ReadBytes(const std::filesystem::path& path, std::size_t limit) {
    std::ifstream stream(path, std::ios::binary);
    Require(stream.good(), "Cannot read preview artifact");
    Bytes bytes;
    std::array<char, 65536> buffer{};
    while (stream) {
        stream.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const auto count = static_cast<std::size_t>(stream.gcount());
        Require(count <= limit - bytes.size(), "Preview artifact exceeds its size limit");
        bytes.insert(bytes.end(), buffer.data(), buffer.data() + count);
    }
    Require(stream.eof(), "Preview artifact could not be read completely");
    return bytes;
}

Json Parse(const Bytes& bytes) {
    std::vector<std::set<std::string>> keys;
    return Json::parse(bytes.begin(), bytes.end(), [&keys](int depth, Json::parse_event_t event, Json& item) {
        Require(depth <= 64, "Preview JSON is nested too deeply");
        if (event == Json::parse_event_t::object_start) keys.emplace_back();
        if (event == Json::parse_event_t::key)
            Require(keys.back().insert(item.get<std::string>()).second, "Preview JSON contains a duplicate key");
        if (event == Json::parse_event_t::object_end) keys.pop_back();
        return true;
    });
}

std::string String(const Json& value, std::size_t limit = 1024) {
    Require(value.is_string(), "Preview metadata requires a string");
    auto text = value.get<std::string>();
    Require(text.size() <= limit && text.find('\0') == std::string::npos, "Preview metadata string is invalid or too long");
    return text;
}

std::string Digest(const Json& value) {
    auto text = String(value, 64);
    Require(IsHash(text), "Preview artifact SHA-256 is malformed");
    return text;
}

const Json& Artifact(const Json& manifest, const std::string& name) {
    Require(manifest.is_object() && manifest.contains("artifacts") && manifest["artifacts"].is_object()
                && manifest["artifacts"].contains(name), "The result does not bind the requested preview artifact");
    const auto& item = manifest["artifacts"][name];
    Require(item.is_object() && item.contains("path") && item.contains("sha256"), "Preview artifact binding is incomplete");
    return item;
}

Bytes ReadBound(const std::filesystem::path& directory, const Json& artifact, std::size_t limit) {
    auto bytes = ReadBytes(Contained(directory, String(artifact.at("path"), 4096)), limit);
    Require(Hash(bytes) == Digest(artifact.at("sha256")), "Preview artifact changed after validation");
    return bytes;
}

double Number(const Json& value, double limit = 1000000) {
    Require(value.is_number(), "Preview geometry requires a number");
    double number = value.get<double>();
    Require(std::isfinite(number) && std::abs(number) <= limit, "Preview geometry is nonfinite or outside its bounds");
    return number;
}

int Integer(const Json& value, int low, int high) {
    Require(value.is_number_integer(), "Preview geometry requires an integer");
    if (value.is_number_unsigned())
        Require(value.get<std::uint64_t>() <= static_cast<std::uint64_t>(high), "Preview integer is outside its bounds");
    auto number = value.get<std::int64_t>();
    Require(number >= low && number <= high, "Preview integer is outside its bounds");
    return static_cast<int>(number);
}

wxPoint2DDouble Point(const Json& value) {
    return {static_cast<double>(Integer(value.at("x"), -1000000, 1000000)),
            static_cast<double>(Integer(value.at("y"), -1000000, 1000000))};
}

struct Bounds {
    wxPoint2DDouble minimum, maximum;
    bool Contains(const wxPoint2DDouble& point) const {
        return point.m_x >= minimum.m_x && point.m_x <= maximum.m_x
            && point.m_y >= minimum.m_y && point.m_y <= maximum.m_y;
    }
};

Bounds ReadBounds(const Json& value) {
    Bounds result{Point(value.at("min")), Point(value.at("max"))};
    Require(result.minimum.m_x <= result.maximum.m_x && result.minimum.m_y <= result.maximum.m_y,
            "Preview component bounds are reversed");
    return result;
}

struct Affine {
    double a = 1, b = 0, c = 0, d = 1, tx = 0, ty = 0;
    wxPoint2DDouble Apply(const wxPoint2DDouble& point) const {
        return {a * point.m_x + c * point.m_y + tx, b * point.m_x + d * point.m_y + ty};
    }
    wxPoint2DDouble Inverse(const wxPoint2DDouble& point) const {
        double x = point.m_x - tx, y = point.m_y - ty, determinant = a * d - b * c;
        return {(d * x - c * y) / determinant, (-b * x + a * y) / determinant};
    }
};

struct Instance {
    std::string id, type, label;
    Bounds bounds, body;
    std::vector<wxPoint2DDouble> pins;
};

struct View {
    Json imageArtifact;
    std::string imageHash;
    wxSize size;
    Affine transform;
    std::vector<Instance> instances;
    wxBitmap bitmap;
    bool metadata = false;
};

wxSize PngSize(const Bytes& bytes) {
    static const unsigned char signature[] = {137, 80, 78, 71, 13, 10, 26, 10};
    Require(bytes.size() >= 33 && std::equal(std::begin(signature), std::end(signature), bytes.begin())
            && bytes[8] == 0 && bytes[9] == 0 && bytes[10] == 0 && bytes[11] == 13
            && bytes[12] == 'I' && bytes[13] == 'H' && bytes[14] == 'D' && bytes[15] == 'R',
            "Preview is not a bounded PNG image");
    auto be32 = [&bytes](std::size_t offset) {
        return (std::uint32_t(bytes[offset]) << 24) | (std::uint32_t(bytes[offset + 1]) << 16)
            | (std::uint32_t(bytes[offset + 2]) << 8) | std::uint32_t(bytes[offset + 3]);
    };
    auto width = be32(16), height = be32(20);
    Require(width > 0 && height > 0 && width <= kDimensionLimit && height <= kDimensionLimit
            && std::uint64_t(width) * height <= kPixelLimit, "Preview PNG dimensions exceed the native image limit");
    return {static_cast<int>(width), static_cast<int>(height)};
}

void LoadImage(View& view, const std::filesystem::path& directory) {
    auto bytes = ReadBound(directory, view.imageArtifact, kImageLimit);
    const auto dimensions = PngSize(bytes);
    Require(!view.metadata || dimensions == view.size, "Preview PNG dimensions do not match their metadata");
    if (!wxImage::FindHandler(wxBITMAP_TYPE_PNG)) wxImage::AddHandler(new wxPNGHandler);
    wxMemoryInputStream stream(bytes.data(), bytes.size());
    wxImage image;
    { wxLogNull suppressDecoderDialogs;
      Require(image.LoadFile(stream, wxBITMAP_TYPE_PNG) && image.IsOk(), "Cannot decode the verified preview PNG"); }
    Require(image.GetWidth() == dimensions.x && image.GetHeight() == dimensions.y,
            "Decoded preview dimensions disagree with the PNG header");
    view.size = dimensions;
    view.bitmap = wxBitmap(image);
    Require(view.bitmap.IsOk(), "Cannot create the preview bitmap");
}

View ReadView(const Json& value, const Json& manifest, bool baseline) {
    View result;
    result.metadata = true;
    Require(value.is_object(), "Preview view metadata is malformed");
    if (!value.at("module_id").is_null()) String(value.at("module_id"), 256);
    String(value.at("comparison_scope"), 256);
    const auto& image = value.at("image");
    const std::string label = baseline ? "baseline_preview" : "preview";
    Require(String(image.at("artifact")) == label, "Preview view refers to the wrong image artifact");
    result.imageArtifact = Artifact(manifest, label);
    result.imageHash = Digest(image.at("sha256"));
    Require(result.imageHash == Digest(result.imageArtifact.at("sha256")), "Preview image binding differs from its manifest");
    result.size = {Integer(image.at("width_px"), 1, kDimensionLimit), Integer(image.at("height_px"), 1, kDimensionLimit)};
    Require(std::uint64_t(result.size.x) * result.size.y <= kPixelLimit, "Preview metadata image is too large");
    const auto& circuit = value.at("circuit");
    auto circuitHash = Digest(circuit.at("sha256")), sidecarHash = Digest(circuit.at("sidecar_sha256"));
    if (!baseline) {
        Require(circuitHash == Digest(Artifact(manifest, "circuit").at("sha256"))
                && sidecarHash == Digest(Artifact(manifest, "sidecar").at("sha256")),
                "Preview metadata belongs to a different circuit");
    }
    const auto& matrix = value.at("circuit_to_pixel");
    result.transform = {Number(matrix.at("a")), Number(matrix.at("b")), Number(matrix.at("c")),
                        Number(matrix.at("d")), Number(matrix.at("tx")), Number(matrix.at("ty"))};
    Require(std::abs(result.transform.a * result.transform.d - result.transform.b * result.transform.c) > 1e-20,
            "Preview coordinate transform is singular");
    const auto& instances = value.at("instances");
    Require(instances.is_array() && instances.size() <= 1024, "Preview contains too many components");
    std::set<std::string> ids;
    std::size_t pinCount = 0;
    for (const auto& item : instances) {
        Instance instance;
        instance.id = String(item.at("id"), 128);
        Require(!instance.id.empty() && ids.insert(instance.id).second, "Preview component IDs must be unique");
        instance.type = String(item.at("type"), 256);
        instance.label = String(item.at("label"));
        Point(item.at("position"));
        const auto rotation = Integer(item.at("rotation"), 0, 270);
        Require(rotation % 90 == 0, "Preview component rotation is invalid");
        instance.bounds = ReadBounds(item.at("bounds"));
        instance.body = ReadBounds(item.at("body_bounds"));
        const auto& pins = item.at("pins");
        Require(pins.is_array() && (pinCount += pins.size()) <= 32768, "Preview contains too many pins");
        for (const auto& pin : pins) {
            const auto direction = String(pin.at("direction"));
            Require(direction == "input" || direction == "output" || direction == "both", "Preview pin direction is invalid");
            String(pin.at("pin"), 256);
            if (!pin.at("width").is_null()) Integer(pin.at("width"), 1, 64);
            instance.pins.push_back(Point(pin.at("position")));
        }
        result.instances.push_back(std::move(instance));
    }
    return result;
}

std::map<std::string, std::pair<std::string, std::string>> Identities(const View& view) {
    std::map<std::string, std::pair<std::string, std::string>> result;
    for (const auto& item : view.instances) result.emplace(item.id, std::make_pair(item.type, item.label));
    return result;
}

void SetError(wxString* destination, const std::exception& error) {
    if (destination) *destination = wxString::FromUTF8(error.what());
}
} // namespace

struct PreviewPanel::State {
    std::filesystem::path directory;
    std::optional<View> selected, baseline;
    bool showingBaseline = false, overlays = false, fitted = true;
    bool dragging = false, moved = false;
    wxPoint down, last;
    double zoom = 1;
    wxPoint2DDouble offset;
    std::string selection;
    std::function<void(std::string)> callback;
    View* Current() { return showingBaseline && baseline ? &*baseline : selected ? &*selected : nullptr; }
    const View* Current() const { return showingBaseline && baseline ? &*baseline : selected ? &*selected : nullptr; }
};

PreviewPanel::PreviewPanel(wxWindow* parent, wxWindowID id)
    : wxPanel(parent, id, wxDefaultPosition, wxDefaultSize, wxBORDER_NONE | wxWANTS_CHARS), state_(std::make_unique<State>()) {
    SetBackgroundStyle(wxBG_STYLE_PAINT);
    SetName("Circuit preview");
    SetToolTip("Drag to pan. Use the mouse wheel or +/- to zoom; 0 fits the circuit. Click a component to inspect it.");
    Bind(wxEVT_PAINT, &PreviewPanel::OnPaint, this);
    Bind(wxEVT_SIZE, &PreviewPanel::OnSize, this);
    Bind(wxEVT_LEFT_DOWN, &PreviewPanel::OnMouseDown, this);
    Bind(wxEVT_MIDDLE_DOWN, &PreviewPanel::OnMouseDown, this);
    Bind(wxEVT_LEFT_UP, &PreviewPanel::OnMouseUp, this);
    Bind(wxEVT_MIDDLE_UP, &PreviewPanel::OnMouseUp, this);
    Bind(wxEVT_MOTION, &PreviewPanel::OnMouseMove, this);
    Bind(wxEVT_MOUSEWHEEL, &PreviewPanel::OnMouseWheel, this);
    Bind(wxEVT_MOUSE_CAPTURE_LOST, &PreviewPanel::OnCaptureLost, this);
    Bind(wxEVT_KEY_DOWN, &PreviewPanel::OnKey, this);
}

PreviewPanel::~PreviewPanel() {
    if (HasCapture()) ReleaseMouse();
}

bool PreviewPanel::LoadResult(const std::filesystem::path& directory, const Json& manifest,
                               const Json& suppliedMetadata, wxString* error) {
    Clear();
    if (error) error->clear();
    try {
        auto root = std::filesystem::canonical(directory);
        Require(std::filesystem::is_directory(root), "Preview result directory does not exist");
        Require(Parse(ReadBytes(Contained(root, "manifest.json"), kJsonLimit)) == manifest,
                "The result manifest changed before preview loading");
        const auto& artifacts = manifest.at("artifacts");
        View selected;
        std::optional<View> baseline;
        if (artifacts.contains("preview_metadata")) {
            auto metadata = Parse(ReadBound(root, Artifact(manifest, "preview_metadata"), kJsonLimit));
            Require(suppliedMetadata.is_null() || suppliedMetadata == metadata,
                    "The preview metadata changed before loading");
            Require(metadata.is_object() && Integer(metadata.at("schema_version"), 1, 1) == 1,
                    "Unsupported preview metadata version");
            String(metadata.at("top"));
            selected = ReadView(metadata.at("views").at("selected"), manifest, false);
            // Selected metadata is bound to the current editable circuit pair.
            ReadBound(root, Artifact(manifest, "circuit"), 32 * 1024 * 1024);
            ReadBound(root, Artifact(manifest, "sidecar"), kJsonLimit);
            if (metadata.at("views").contains("baseline")) {
                baseline = ReadView(metadata["views"]["baseline"], manifest, true);
                Require(Identities(selected) == Identities(*baseline), "Baseline and selected preview IDs do not describe the same components");
                Require(metadata["views"]["selected"]["module_id"] == metadata["views"]["baseline"]["module_id"],
                        "Baseline and selected previews belong to different canvases");
            }
        } else {
            Require(suppliedMetadata.is_null() || suppliedMetadata.empty(), "Unbound preview metadata cannot provide component overlays");
            selected.imageArtifact = Artifact(manifest, "preview");
            if (artifacts.contains("baseline_preview")) {
                baseline.emplace();
                baseline->imageArtifact = Artifact(manifest, "baseline_preview");
            }
        }
        LoadImage(selected, root);
        state_->directory = std::move(root);
        state_->selected = std::move(selected);
        state_->baseline = std::move(baseline);
        Fit();
        return true;
    } catch (const std::exception& exception) {
        Clear();
        SetError(error, exception);
        return false;
    }
}

bool PreviewPanel::ShowBaseline(bool show, wxString* error) {
    if (error) error->clear();
    if (show && !state_->baseline) {
        if (error) *error = "This result has no baseline preview.";
        return false;
    }
    if (state_->showingBaseline == show) return true;
    try {
        if (show && !state_->baseline->bitmap.IsOk()) LoadImage(*state_->baseline, state_->directory);
        state_->showingBaseline = show;
        Fit();
        return true;
    } catch (const std::exception& exception) {
        SetError(error, exception);
        return false;
    }
}

void PreviewPanel::Clear() {
    if (HasCapture()) ReleaseMouse();
    const bool selected = !state_->selection.empty();
    auto callback = std::move(state_->callback);
    bool overlays = state_->overlays;
    state_ = std::make_unique<State>();
    state_->callback = std::move(callback);
    state_->overlays = overlays;
    if (selected) NotifySelection();
    Refresh();
}

void PreviewPanel::Fit() {
    const auto* view = state_->Current();
    if (!view || !view->bitmap.IsOk()) return;
    const auto client = GetClientSize();
    const double padding = FromDIP(16);
    const double width = std::max(1.0, client.x - 2 * padding), height = std::max(1.0, client.y - 2 * padding);
    state_->zoom = std::clamp(std::min(width / view->size.x, height / view->size.y), kMinimumZoom, kMaximumZoom);
    state_->offset = {(client.x - view->size.x * state_->zoom) / 2, (client.y - view->size.y * state_->zoom) / 2};
    state_->fitted = true;
    Refresh();
}

void PreviewPanel::ZoomAt(double factor, const wxPoint& clientPoint) {
    if (!state_->Current() || !std::isfinite(factor) || factor <= 0) return;
    auto anchor = ClientToImage({static_cast<double>(clientPoint.x), static_cast<double>(clientPoint.y)});
    state_->zoom = std::clamp(state_->zoom * factor, kMinimumZoom, kMaximumZoom);
    state_->offset = {clientPoint.x - anchor.m_x * state_->zoom, clientPoint.y - anchor.m_y * state_->zoom};
    state_->fitted = false;
    Refresh();
}

void PreviewPanel::PanBy(const wxPoint& delta) {
    if (!state_->Current()) return;
    state_->offset.m_x += delta.x;
    state_->offset.m_y += delta.y;
    state_->fitted = false;
    Refresh();
}

void PreviewPanel::SetOverlays(bool enabled) { state_->overlays = enabled; Refresh(); }
void PreviewPanel::SetSelectionCallback(std::function<void(std::string)> callback) { state_->callback = std::move(callback); }
double PreviewPanel::GetZoom() const { return state_->zoom; }
std::string PreviewPanel::GetSelection() const { return state_->selection; }
bool PreviewPanel::HasBaseline() const { return state_->baseline.has_value(); }
bool PreviewPanel::IsShowingBaseline() const { return state_->showingBaseline; }
bool PreviewPanel::IsBaselineLoaded() const { return state_->baseline && state_->baseline->bitmap.IsOk(); }
bool PreviewPanel::HasMetadata() const { return state_->Current() && state_->Current()->metadata; }
wxSize PreviewPanel::GetImageSize() const { return state_->Current() ? state_->Current()->size : wxSize(0, 0); }
wxPoint2DDouble PreviewPanel::ImageToClient(const wxPoint2DDouble& point) const {
    return {point.m_x * state_->zoom + state_->offset.m_x, point.m_y * state_->zoom + state_->offset.m_y};
}
wxPoint2DDouble PreviewPanel::ClientToImage(const wxPoint2DDouble& point) const {
    return {(point.m_x - state_->offset.m_x) / state_->zoom, (point.m_y - state_->offset.m_y) / state_->zoom};
}

std::string PreviewPanel::HitTest(const wxPoint& clientPoint) const {
    const auto* view = state_->Current();
    if (!view || !view->metadata) return {};
    const wxPoint2DDouble client(clientPoint.x, clientPoint.y);
    const auto native = view->transform.Inverse(ClientToImage(client));
    const double tolerance = FromDIP(6);
    for (auto iterator = view->instances.rbegin(); iterator != view->instances.rend(); ++iterator) {
        for (const auto& pin : iterator->pins) {
            const auto screen = ImageToClient(view->transform.Apply(pin));
            if (std::hypot(screen.m_x - client.m_x, screen.m_y - client.m_y) <= tolerance) return iterator->id;
        }
        if (iterator->body.Contains(native)) return iterator->id;
    }
    return {};
}

void PreviewPanel::SelectAt(const wxPoint& point) {
    const auto selected = HitTest(point);
    if (state_->selection != selected) {
        state_->selection = selected;
        NotifySelection();
        Refresh();
    }
}

void PreviewPanel::NotifySelection() { if (state_->callback) state_->callback(state_->selection); }

void PreviewPanel::OnPaint(wxPaintEvent&) {
    wxAutoBufferedPaintDC dc(this);
    dc.SetBackground(wxBrush(wxColour(235, 238, 243)));
    dc.Clear();
    const auto* view = state_->Current();
    if (!view || !view->bitmap.IsOk()) {
        dc.SetTextForeground(wxColour(85, 96, 110));
        dc.DrawText("Convert a Verilog project to preview its circuit.", FromDIP(24), FromDIP(24));
        return;
    }
    std::unique_ptr<wxGraphicsContext> graphics(wxGraphicsContext::Create(dc));
    if (!graphics) return;
    graphics->SetInterpolationQuality(wxINTERPOLATION_BEST);
    graphics->DrawBitmap(view->bitmap, state_->offset.m_x, state_->offset.m_y,
                         view->size.x * state_->zoom, view->size.y * state_->zoom);
    if (!view->metadata) return;
    for (const auto& item : view->instances) {
        const bool selected = item.id == state_->selection;
        if (!state_->overlays && !selected) continue;
        std::array<wxPoint2DDouble, 4> corners{{item.body.minimum,
            {item.body.maximum.m_x, item.body.minimum.m_y}, item.body.maximum,
            {item.body.minimum.m_x, item.body.maximum.m_y}}};
        for (auto& corner : corners) corner = ImageToClient(view->transform.Apply(corner));
        auto path = graphics->CreatePath();
        path.MoveToPoint(corners[0].m_x, corners[0].m_y);
        for (std::size_t index = 1; index < corners.size(); ++index) path.AddLineToPoint(corners[index].m_x, corners[index].m_y);
        path.CloseSubpath();
        const wxColour colour = selected ? wxColour(215, 104, 0) : wxColour(30, 96, 180);
        graphics->SetPen(wxPen(colour, FromDIP(selected ? 2 : 1)));
        graphics->StrokePath(path);
        if (selected || state_->overlays) {
            graphics->SetBrush(wxBrush(colour));
            const double radius = FromDIP(selected ? 3 : 2);
            for (const auto& pin : item.pins) {
                const auto point = ImageToClient(view->transform.Apply(pin));
                graphics->DrawEllipse(point.m_x - radius, point.m_y - radius, radius * 2, radius * 2);
            }
        }
        if (state_->overlays || selected) {
            const auto text = wxString::FromUTF8((item.label.empty() ? item.type : item.label) + " [" + item.id + "]");
            graphics->SetFont(GetFont(), colour);
            graphics->DrawText(text, corners[0].m_x + FromDIP(3), corners[0].m_y - FromDIP(18));
        }
    }
}

void PreviewPanel::OnSize(wxSizeEvent& event) { if (state_->fitted) Fit(); else Refresh(); event.Skip(); }
void PreviewPanel::OnMouseDown(wxMouseEvent& event) {
    SetFocus();
    if (!state_->Current()) return;
    state_->down = state_->last = event.GetPosition();
    state_->dragging = true;
    state_->moved = false;
    if (!HasCapture()) CaptureMouse();
    SetCursor(wxCursor(wxCURSOR_HAND));
}
void PreviewPanel::OnMouseUp(wxMouseEvent& event) {
    if (!state_->dragging) return;
    const bool click = !state_->moved && event.LeftUp();
    state_->dragging = false;
    if (HasCapture()) ReleaseMouse();
    SetCursor(wxNullCursor);
    if (click) SelectAt(event.GetPosition());
}
void PreviewPanel::OnMouseMove(wxMouseEvent& event) {
    if (!state_->dragging) return;
    const auto point = event.GetPosition();
    if (std::abs(point.x - state_->down.x) + std::abs(point.y - state_->down.y) > FromDIP(3)) state_->moved = true;
    if (state_->moved) PanBy(point - state_->last);
    state_->last = point;
}
void PreviewPanel::OnMouseWheel(wxMouseEvent& event) {
    if (event.GetWheelDelta() != 0)
        ZoomAt(std::pow(1.2, static_cast<double>(event.GetWheelRotation()) / event.GetWheelDelta()), event.GetPosition());
}
void PreviewPanel::OnCaptureLost(wxMouseCaptureLostEvent&) { state_->dragging = false; SetCursor(wxNullCursor); }
void PreviewPanel::OnKey(wxKeyEvent& event) {
    const int key = event.GetKeyCode(), step = FromDIP(40);
    const auto size = GetClientSize();
    if (key == '+' || key == '=' || key == WXK_ADD || key == WXK_NUMPAD_ADD) ZoomAt(1.2, {size.x / 2, size.y / 2});
    else if (key == '-' || key == WXK_SUBTRACT || key == WXK_NUMPAD_SUBTRACT) ZoomAt(1 / 1.2, {size.x / 2, size.y / 2});
    else if (key == '0' || key == WXK_NUMPAD0 || key == WXK_HOME) Fit();
    else if (key == WXK_LEFT) PanBy({step, 0});
    else if (key == WXK_RIGHT) PanBy({-step, 0});
    else if (key == WXK_UP) PanBy({0, step});
    else if (key == WXK_DOWN) PanBy({0, -step});
    else if (key == WXK_TAB) Navigate(event.ShiftDown() ? wxNavigationKeyEvent::IsBackward : wxNavigationKeyEvent::IsForward);
    else event.Skip();
}
