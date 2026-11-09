#include "preview_panel.h"

#include <wx/wx.h>
#include <wx/mstream.h>
#include <wx/msw/wrapwin.h>
#include <bcrypt.h>
#include <nlohmann/json.hpp>

#include <array>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>

using Json = nlohmann::json;
using Bytes = std::vector<unsigned char>;

class PreviewTestApp : public wxApp {
public:
    bool OnInit() override { wxInitAllImageHandlers(); return true; }
};
wxIMPLEMENT_APP_NO_MAIN(PreviewTestApp);

void Check(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

void Near(double actual, double expected, const char* message) {
    Check(std::abs(actual - expected) < .000001, message);
}

Bytes Read(const std::filesystem::path& path) {
    std::ifstream stream(path, std::ios::binary);
    return Bytes(std::istreambuf_iterator<char>(stream), {});
}

void Write(const std::filesystem::path& path, const Bytes& bytes) {
    std::ofstream stream(path, std::ios::binary);
    stream.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    Check(stream.good(), "test fixture write failed");
}

void WriteJson(const std::filesystem::path& path, const Json& value) {
    auto text = value.dump(2);
    Write(path, Bytes(text.begin(), text.end()));
}

std::string Hash(const Bytes& bytes) {
    std::array<unsigned char, 32> digest{};
    BCRYPT_ALG_HANDLE algorithm;
    Check(BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0, "test SHA provider failed");
    auto status = BCryptHash(algorithm, nullptr, 0, const_cast<PUCHAR>(bytes.data()),
                             static_cast<ULONG>(bytes.size()), digest.data(), static_cast<ULONG>(digest.size()));
    BCryptCloseAlgorithmProvider(algorithm, 0);
    Check(status >= 0, "test SHA failed");
    const char* hex = "0123456789abcdef";
    std::string text;
    for (unsigned char value : digest) { text += hex[value >> 4]; text += hex[value & 15]; }
    return text;
}

Json Point(int x, int y) { return {{"x", x}, {"y", y}}; }
Json Bounds(int x1, int y1, int x2, int y2) { return {{"min", Point(x1, y1)}, {"max", Point(x2, y2)}}; }

struct Fixture {
    std::filesystem::path directory;
    Json manifest, metadata;

    Fixture(const std::filesystem::path& root, const std::string& name, bool legacy = false) : directory(root / name) {
        std::filesystem::create_directories(directory);
        Write(directory / "circuit.dig", {'a', 'b', 'c'});
        Write(directory / "circuit.dig.ids.json", {'{', '}'});
        SavePng("preview.png", 200, 200);
        SavePng("baseline-preview.png", 300, 180);
        manifest = {{"schema_version", 2}, {"state", "ready"}, {"artifacts", Json::object()}};
        Bind("circuit", "circuit.dig"); Bind("sidecar", "circuit.dig.ids.json");
        Bind("preview", "preview.png"); Bind("baseline_preview", "baseline-preview.png");
        Json instance = {{"id", "g0000"}, {"type", "And"}, {"label", "data"}, {"position", Point(10, 20)},
            {"rotation", 90}, {"bounds", Bounds(5, 15, 55, 65)}, {"body_bounds", Bounds(10, 20, 50, 60)},
            {"pins", Json::array({{{"direction", "input"}, {"pin", "in1"}, {"position", Point(10, 40)}, {"width", 8}},
                                  {{"direction", "output"}, {"pin", "out"}, {"position", Point(70, 40)}, {"width", 8}}})}};
        Json selected = {{"module_id", nullptr}, {"comparison_scope", "top_canvas"},
            {"image", {{"artifact", "preview"}, {"sha256", manifest["artifacts"]["preview"]["sha256"]},
                       {"width_px", 200}, {"height_px", 200}}},
            {"circuit", {{"sha256", manifest["artifacts"]["circuit"]["sha256"]},
                         {"sidecar_sha256", manifest["artifacts"]["sidecar"]["sha256"]}}},
            {"circuit_to_pixel", {{"a", 0.0}, {"b", 2.0}, {"c", -2.0}, {"d", 0.0}, {"tx", 140.0}, {"ty", 50.0}}},
            {"instances", Json::array({instance})}};
        Json baseline = selected;
        baseline["image"] = {{"artifact", "baseline_preview"}, {"sha256", manifest["artifacts"]["baseline_preview"]["sha256"]},
                             {"width_px", 300}, {"height_px", 180}};
        baseline["circuit_to_pixel"] = {{"a", 1.0}, {"b", 0.0}, {"c", 0.0}, {"d", 1.0}, {"tx", -90.0}, {"ty", 30.0}};
        baseline["instances"][0]["position"] = Point(110, 20);
        baseline["instances"][0]["body_bounds"] = Bounds(110, 20, 150, 60);
        baseline["instances"][0]["bounds"] = Bounds(105, 15, 155, 65);
        baseline["instances"][0]["pins"][0]["position"] = Point(110, 40);
        baseline["instances"][0]["pins"][1]["position"] = Point(170, 40);
        metadata = {{"schema_version", 1}, {"top", "example"}, {"views", {{"selected", selected}, {"baseline", baseline}}}};
        if (legacy) metadata = nullptr;
        Publish();
    }

    void SavePng(const std::string& name, int width, int height) {
        wxImage image(width, height);
        image.SetRGB(wxRect(0, 0, width, height), 255, 255, 255);
        image.SetRGB(wxRect(20, 70, 80, 80), 20, 80, 160);
        wxMemoryOutputStream stream;
        Check(image.SaveFile(stream, wxBITMAP_TYPE_PNG), "test PNG generation failed");
        Bytes bytes(stream.GetSize());
        stream.CopyTo(bytes.data(), bytes.size());
        Write(directory / name, bytes);
    }

    void Bind(const std::string& label, const std::string& filename) {
        manifest["artifacts"][label] = {{"path", filename}, {"sha256", Hash(Read(directory / filename))}};
    }

    void Publish() {
        if (!metadata.is_null()) {
            WriteJson(directory / "preview-metadata.json", metadata);
            Bind("preview_metadata", "preview-metadata.json");
        }
        WriteJson(directory / "manifest.json", manifest);
    }
};

wxPoint Pixel(PreviewPanel& panel, double x, double y) {
    const auto point = panel.ImageToClient({x, y});
    return {static_cast<int>(std::round(point.m_x)), static_cast<int>(std::round(point.m_y))};
}

int main(int argc, char** argv) {
    if (!wxEntryStart(argc, argv)) return 2;
    if (!wxTheApp->CallOnInit()) { wxEntryCleanup(); return 2; }
    int result = 0;
    wxFrame* frame = nullptr;
    try {
        Check(argc >= 2, "provide a workspace-local artifact directory");
        auto root = std::filesystem::u8path(argv[1]) / ("preview-" + std::to_string(GetCurrentProcessId()) + "-"
            + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
        std::filesystem::create_directories(root);
        frame = new wxFrame(nullptr, wxID_ANY, "Preview tests", wxDefaultPosition, wxSize(900, 650));
        auto* panel = new PreviewPanel(frame);
        panel->SetSize(0, 0, 800, 600);
        std::vector<std::string> selections;
        panel->SetSelectionCallback([&](std::string id) { selections.push_back(std::move(id)); });
        wxString error;
        Check(Hash({'a', 'b', 'c'}) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "test SHA known vector");

        Fixture good(root, "good");
        Check(panel->LoadResult(good.directory, good.manifest, good.metadata, &error), error.ToStdString());
        Check(panel->HasMetadata() && panel->HasBaseline() && !panel->IsBaselineLoaded(), "baseline should be lazy");
        Check(panel->GetImageSize() == wxSize(200, 200), "selected image dimensions");
        Check(panel->HitTest(Pixel(*panel, 60, 110)) == "g0000", "rotated native body hit testing");
        Check(panel->HitTest(Pixel(*panel, 60, 70)) == "g0000", "rotated native pin hit testing");
        Check(panel->HitTest(Pixel(*panel, 60, 190)) == "g0000", "pin outside body remains selectable");
        Check(panel->HitTest(Pixel(*panel, 180, 180)).empty(), "outside body hit testing");
        panel->SelectAt(Pixel(*panel, 60, 110));
        Check(panel->GetSelection() == "g0000" && selections.back() == "g0000", "selection callback");

        const wxPoint anchor(217, 163);
        const auto before = panel->ClientToImage({217, 163});
        const auto zoom = panel->GetZoom();
        panel->ZoomAt(1.75, anchor);
        Near(panel->GetZoom(), zoom * 1.75, "zoom factor");
        const auto after = panel->ClientToImage({217, 163});
        Near(before.m_x, after.m_x, "cursor anchor x"); Near(before.m_y, after.m_y, "cursor anchor y");
        const auto screen = panel->ImageToClient({60, 110});
        panel->PanBy({40, -20});
        const auto shifted = panel->ImageToClient({60, 110});
        Near(shifted.m_x, screen.m_x + 40, "pan x"); Near(shifted.m_y, screen.m_y - 20, "pan y");
        Check(panel->HitTest(Pixel(*panel, 60, 110)) == "g0000", "hit test after zoom and pan");
        panel->ZoomAt(std::numeric_limits<double>::quiet_NaN(), anchor);
        Near(panel->GetZoom(), zoom * 1.75, "ignore nonfinite zoom");
        panel->Fit();
        Check(panel->ShowBaseline(true, &error), error.ToStdString());
        Check(panel->IsBaselineLoaded() && panel->IsShowingBaseline() && panel->GetImageSize() == wxSize(300, 180), "baseline image switch");
        Check(panel->GetSelection() == "g0000" && panel->HitTest(Pixel(*panel, 40, 70)) == "g0000", "baseline preserves ID with different transform");
        Check(panel->ShowBaseline(false), "selected switch");
        panel->SetOverlays(true);

        wxKeyEvent key(wxEVT_KEY_DOWN);
        key.m_keyCode = '+';
        const auto keyZoom = panel->GetZoom();
        panel->GetEventHandler()->ProcessEvent(key);
        Near(panel->GetZoom(), keyZoom * 1.2, "keyboard zoom");
        key.m_keyCode = '0'; panel->GetEventHandler()->ProcessEvent(key);
        Near(panel->GetZoom(), zoom, "keyboard fit");

        Fixture late(root, "lazy-corruption");
        Check(panel->LoadResult(late.directory, late.manifest, late.metadata), "load before lazy mutation");
        auto damaged = Read(late.directory / "baseline-preview.png"); damaged.back() ^= 1;
        Write(late.directory / "baseline-preview.png", damaged);
        Check(!panel->ShowBaseline(true, &error) && !error.empty(), "reject changed lazy image");
        Check(!panel->IsShowingBaseline() && panel->GetImageSize() == wxSize(200, 200), "preserve selected after lazy failure");

        Fixture legacy(root, "legacy", true);
        Check(panel->LoadResult(legacy.directory, legacy.manifest, nullptr), "legacy PNG-only preview");
        Check(!panel->HasMetadata() && panel->HitTest(Pixel(*panel, 60, 110)).empty(), "legacy has no guessed overlays");

        Fixture unicode(root, u8"circuit-\u03bc-\u6e2c\u8a66");
        unicode.metadata["views"]["selected"]["instances"][0]["label"] = u8"sum_\u03bc";
        unicode.metadata["views"]["baseline"]["instances"][0]["label"] = u8"sum_\u03bc";
        unicode.Publish();
        Check(panel->LoadResult(unicode.directory, unicode.manifest, unicode.metadata, &error), "Unicode result path/label: " + error.ToStdString());
        Check(panel->HitTest(Pixel(*panel, 60, 110)) == "g0000", "Unicode metadata preserves IDs");

        auto reject = [&](const std::string& name, auto mutate) {
            Fixture bad(root, name);
            mutate(bad);
            Check(!panel->LoadResult(bad.directory, bad.manifest, bad.metadata, &error), "accepted invalid fixture: " + name);
            Check(!error.empty() && panel->GetImageSize() == wxSize(0, 0) && panel->GetSelection().empty(), "failed load did not clear stale preview");
        };
        reject("png-hash", [](Fixture& f) { auto bytes = Read(f.directory / "preview.png"); bytes.back() ^= 1; Write(f.directory / "preview.png", bytes); });
        reject("metadata-hash", [](Fixture& f) { auto m = f.metadata; m["views"]["selected"]["circuit_to_pixel"]["tx"] = 0; WriteJson(f.directory / "preview-metadata.json", m); });
        reject("caller-metadata", [](Fixture& f) { f.metadata["top"] = "stale caller"; });
        reject("image-dimensions", [](Fixture& f) { f.metadata["views"]["selected"]["image"]["width_px"] = 201; f.Publish(); });
        reject("singular-affine", [](Fixture& f) { f.metadata["views"]["selected"]["circuit_to_pixel"]["b"] = 0; f.Publish(); });
        reject("duplicate-ID", [](Fixture& f) { auto item = f.metadata["views"]["selected"]["instances"][0]; f.metadata["views"]["selected"]["instances"].push_back(item); f.Publish(); });
        reject("baseline-identity", [](Fixture& f) { f.metadata["views"]["baseline"]["instances"][0]["label"] = "different"; f.Publish(); });
        reject("circuit-pair", [](Fixture& f) { Write(f.directory / "circuit.dig", {'x'}); });
        reject("traversal", [](Fixture& f) { f.manifest["artifacts"]["preview"]["path"] = "../good/preview.png"; f.Publish(); });
        reject("oversized-PNG", [](Fixture& f) {
            auto bytes = Read(f.directory / "preview.png");
            bytes[16] = 0; bytes[17] = 1; bytes[18] = 0; bytes[19] = 0;
            Write(f.directory / "preview.png", bytes); f.Bind("preview", "preview.png");
            f.metadata["views"]["selected"]["image"]["sha256"] = f.manifest["artifacts"]["preview"]["sha256"]; f.Publish();
        });
        reject("uint64-coordinate", [](Fixture& f) { f.metadata["views"]["selected"]["instances"][0]["position"]["x"] = std::numeric_limits<std::uint64_t>::max(); f.Publish(); });

        Check(panel->LoadResult(good.directory, good.manifest, good.metadata), "reload after failures");
        panel->SelectAt(Pixel(*panel, 60, 110));
        panel->Clear();
        Check(panel->GetImageSize() == wxSize(0, 0) && selections.back().empty(), "clear selection notification");
        for (int argument = 2; argument < argc; ++argument) {
            const auto directory = std::filesystem::u8path(argv[argument]);
            auto manifestBytes = Read(directory / "manifest.json");
            auto metadataBytes = Read(directory / "preview-metadata.json");
            auto manifest = Json::parse(manifestBytes), metadata = Json::parse(metadataBytes);
            Check(panel->LoadResult(directory, manifest, metadata, &error), "native artifact: " + error.ToStdString());
            const auto& view = metadata["views"]["selected"];
            const auto& transform = view["circuit_to_pixel"];
            for (const auto& item : view["instances"]) {
                const auto& body = item["body_bounds"];
                double x = (body["min"]["x"].get<double>() + body["max"]["x"].get<double>()) / 2;
                double y = (body["min"]["y"].get<double>() + body["max"]["y"].get<double>()) / 2;
                double pixelX = transform["a"].get<double>() * x + transform["c"].get<double>() * y + transform["tx"].get<double>();
                double pixelY = transform["b"].get<double>() * x + transform["d"].get<double>() * y + transform["ty"].get<double>();
                Check(!panel->HitTest(Pixel(*panel, pixelX, pixelY)).empty(), "native component center has no overlay hit");
            }
            if (view["instances"].size()) {
                panel->ZoomAt(2, {200, 200});
                Check(panel->GetZoom() > 0, "native preview zoom");
            }
            if (metadata["views"].contains("baseline"))
                Check(panel->ShowBaseline(true, &error), "native baseline: " + error.ToStdString());
        }
        std::cout << "Preview tests passed: transforms, navigation, baseline, legacy, artifact binding and malformed inputs\n";
        std::cout << "Artifacts: " << root.u8string() << '\n';
    } catch (const std::exception& error) {
        std::cerr << "Preview test failed: " << error.what() << '\n';
        result = 1;
    }
    delete frame;
    wxTheApp->OnExit();
    wxEntryCleanup();
    return result;
}
