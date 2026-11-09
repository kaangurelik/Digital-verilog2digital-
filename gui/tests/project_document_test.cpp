#include "project_document.h"
#include <windows.h>

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

using Json = nlohmann::json;

void Check(bool condition, const std::string& message) { if (!condition) throw std::runtime_error(message); }
void Bytes(const std::filesystem::path& file, const std::string& bytes) {
    std::ofstream stream(file, std::ios::binary);
    stream.write(bytes.data(), bytes.size());
    Check(stream.good(), "fixture write failed");
}
std::string Read(const std::filesystem::path& file) {
    std::ifstream stream(file, std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(stream), {});
}
template<typename F> void Reject(F operation, const std::string& message) {
    bool rejected = false;
    try { operation(); } catch (const std::exception&) { rejected = true; }
    Check(rejected, message);
}

int main(int argc, char** argv) {
    try {
        Check(argc == 2, "provide a workspace-local artifact directory");
        auto root = std::filesystem::absolute(std::filesystem::u8path(argv[1])) / ("project-" + std::to_string(GetCurrentProcessId()) + "-"
            + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
        auto directory = root / std::filesystem::u8path(u8"project-\u03bc");
        std::filesystem::create_directories(directory);
        std::filesystem::create_directories(root / "headers with spaces");
        std::filesystem::create_directories(root / "hdl");
        auto source = root / std::filesystem::u8path(u8"hdl/\u6e2c\u8a66.v");
        Bytes(source, "module example; endmodule\n");
        Json project = {{"schema_version", 1}, {"sources", {u8"../hdl/\u6e2c\u8a66.v"}}, {"top", "example"},
            {"include_dirs", {"../headers with spaces"}}, {"defines", {{"FLAG", ""}, {"SYNTHESIS", "1"}}},
            {"parameters", {{"WIDTH", 8}, {"VALUE", "8'hff"}}}, {"output", "../result"}, {"trace", "../clock cycles.json"},
            {"conversion", {{"flatten", false}, {"layout_strategy", "auto"}, {"spacing", 40},
                {"refinement", {{"mode", "off"}, {"context_tokens", nullptr}, {"model_memory_mib", 6144}, {"idle_seconds", 30}}}}},
            {"resources", {{"memory_mib", 6144}, {"process_memory_mib", nullptr}}}};
        auto file = directory / "project.json";
        v2d::WriteJson(file, project, false);
        auto loaded = v2d::LoadProject(file);
        Check(std::filesystem::u8path(loaded["sources"][0].get<std::string>()) == source, "Unicode relative source resolution");
        Check(std::filesystem::u8path(loaded["trace"].get<std::string>()) == root / "clock cycles.json", "relative trace resolution");
        Check(loaded["defines"] == project["defines"] && loaded["parameters"] == project["parameters"], "source settings changed");
        Check(v2d::Path(v2d::Display(source)) == source, "wide filesystem path roundtrip");
        Check(v2d::Utf8(v2d::Wide(u8"\u03bc_\u6e2c\u8a66")) == u8"\u03bc_\u6e2c\u8a66", "Unicode label roundtrip");

        auto copy = root / "copies/deeper/saved.json";
        std::filesystem::create_directories(copy.parent_path());
        auto relative = v2d::RelativeProject(loaded, copy);
        Check(!std::filesystem::u8path(relative["sources"][0].get<std::string>()).is_absolute(), "saved source should be relative");
        v2d::WriteJson(copy, relative, false);
        Check(v2d::LoadProject(copy) == loaded, "Save As changed project path meaning");
        auto original = Read(copy);
        Reject([&] { v2d::WriteJson(copy, Json{{"other", true}}, false); }, "save collision overwrote an existing project");
        Check(Read(copy) == original, "failed collision altered bytes");
        auto changed = relative; changed["top"] = "changed";
        v2d::WriteJson(copy, changed, true);
        Check(v2d::LoadProject(copy)["top"] == "changed", "explicit replacement failed");
        original = Read(copy);
        auto handle = CreateFileW(copy.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
        Check(handle != INVALID_HANDLE_VALUE, "cannot hold sharing-denial reader");
        bool denied = false;
        try { v2d::WriteJson(copy, relative, true); } catch (const std::exception&) { denied = true; }
        CloseHandle(handle);
        Check(denied && Read(copy) == original, "failed atomic replacement changed the previous project");
        for (const auto& entry : std::filesystem::directory_iterator(copy.parent_path()))
            Check(entry.path().filename().wstring().find(L".tmp-") == std::wstring::npos, "failed save leaked temporary output");

        Bytes(root / "hash.txt", "abc");
        Check(v2d::Sha256File(root / "hash.txt") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "SHA-256 known vector");
        Reject([&] { v2d::Sha256File(root / "hash.txt", 2); }, "hashing exceeded byte limit");
        Reject([&] { v2d::Sha256File(root / "hash.txt", std::numeric_limits<std::size_t>::max()); }, "unbounded hashing limit accepted");
        Reject([&] { v2d::ReadJson(file, 8); }, "JSON byte limit ignored");

        std::vector<Json> invalid;
        auto variant = [&](const std::string& key, const Json& value) { auto test = project; test[key] = value; invalid.push_back(std::move(test)); };
        for (const auto& value : std::vector<Json>{true, 1.5, 1.0, std::uint64_t(4294967297), "1", nullptr}) variant("schema_version", value);
        variant("unknown", true); variant("sources", Json::array()); variant("sources", "source.v");
        variant("sources", {"same.v", "./same.v"}); variant("sources", {"same.v", "SAME.V"});
        variant("sources", {"C:drive-relative.v"}); variant("sources", {"\\root-relative.v"});
        variant("sources", {std::string("a\0b.v", 5)}); variant("sources", {"a\nb.v"});
        variant("include_dirs", {{"not", "a path"}}); variant("top", true); variant("top", "bad name");
        variant("defines", {{"bad-name", "1"}}); variant("defines", {{"VALUE", 1}});
        variant("defines", {{"VALUE", "a\nb"}}); variant("defines", {{"YOSYS", "0"}});
        variant("parameters", {{"WIDTH", true}}); variant("parameters", {{"WIDTH", 1.5}});
        variant("conversion", {{"spacing", 41}}); variant("conversion", {{"flatten", 1}});
        variant("conversion", {{"layout_timeout", 0}}); variant("conversion", {{"unknown", true}});
        variant("conversion", {{"refinement", {{"mode", "local"}, {"responses", {"{}"}}}}});
        variant("conversion", {{"refinement", {{"context_tokens", true}}}});
        variant("conversion", {{"refinement", {{"model_id", "unknown"}}}});
        variant("resources", {{"memory_mib", 1}}); variant("resources", {{"process_memory_mib", true}});
        variant("resources", {{"deadline_seconds", "30"}}); variant("trace", nullptr);
        for (std::size_t index = 0; index < invalid.size(); ++index) {
            auto bad = root / ("invalid-" + std::to_string(index) + ".json");
            Bytes(bad, invalid[index].dump());
            Reject([&] { v2d::LoadProject(bad); }, "invalid field/type accepted at index" + std::to_string(index));
        }
        auto malformed = root / "malformed.json";
        Bytes(malformed, R"({"schema_version":1,"sources":["a.v"],"top":"a","top":"b"})");
        Reject([&] { v2d::LoadProject(malformed); }, "duplicate JSON key accepted");
        Bytes(malformed, std::string(66, '[') + "0" + std::string(66, ']'));
        Reject([&] { v2d::ReadJson(malformed); }, "JSON depth limit ignored");
        Bytes(malformed, std::string("{\"name\":\"") + char(0xff) + "\"}");
        Reject([&] { v2d::ReadJson(malformed); }, "invalid UTF-8 accepted");
        std::cout << "Project tests passed: Unicode, relative Save As, atomic save/collision, SHA binding and " << invalid.size() << " invalid field/type cases\n";
        std::cout << "Artifacts: " << root.u8string() << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Project test failed: " << error.what() << '\n';
        return 1;
    }
}
