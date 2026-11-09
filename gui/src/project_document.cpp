#include "project_document.h"
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <fstream>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <set>
#include <stdexcept>

namespace v2d {
std::string Utf8(const wxString& value) { auto b = value.ToUTF8(); return {b.data(), b.length()}; }
wxString Wide(const std::string& value) { return wxString::FromUTF8(value.data(), value.size()); }
std::filesystem::path Path(const wxString& value) { return std::filesystem::path(value.ToStdWstring()); }
wxString Display(const std::filesystem::path& value) { return wxString(value.wstring()); }
std::string Sha256File(const std::filesystem::path& file, std::size_t maxBytes) {
    if (maxBytes == 0 || maxBytes > 512 * 1024 * 1024) throw std::runtime_error("Invalid artifact hashing limit");
    std::ifstream input(file, std::ios::binary);
    if (!input) throw std::runtime_error("Cannot open the artifact for SHA-256 verification");
    struct Handles {
        BCRYPT_ALG_HANDLE algorithm = nullptr;
        BCRYPT_HASH_HANDLE hash = nullptr;
        ~Handles() { if (hash) BCryptDestroyHash(hash); if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0); }
    } handles;
    auto success = [](NTSTATUS status) { if (status < 0) throw std::runtime_error("Artifact SHA-256 verification failed"); };
    success(BCryptOpenAlgorithmProvider(&handles.algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0));
    success(BCryptCreateHash(handles.algorithm, &handles.hash, nullptr, 0, nullptr, 0, 0));
    std::array<unsigned char, 65536> buffer{};
    std::size_t total = 0;
    while (input) {
        input.read(reinterpret_cast<char*>(buffer.data()), buffer.size());
        const auto count = static_cast<std::size_t>(input.gcount());
        if (count > maxBytes - total) throw std::runtime_error("Artifact exceeds its SHA-256 size limit");
        total += count;
        if (count) success(BCryptHashData(handles.hash, buffer.data(), static_cast<ULONG>(count), 0));
    }
    if (!input.eof()) throw std::runtime_error("Could not read the complete artifact for SHA-256 verification");
    std::array<unsigned char, 32> result{};
    success(BCryptFinishHash(handles.hash, result.data(), static_cast<ULONG>(result.size()), 0));
    static const char hex[] = "0123456789abcdef";
    std::string text;
    for (auto byte : result) { text += hex[byte >> 4]; text += hex[byte & 15]; }
    return text;
}
nlohmann::json ReadJson(const std::filesystem::path& file, std::size_t limit) {
    if (limit == 0 || limit > 64 * 1024 * 1024) throw std::runtime_error("Invalid JSON read limit");
    std::ifstream input(file, std::ios::binary);
    if (!input) throw std::runtime_error("Cannot open the JSON file");
    std::string data(limit + 1, '\0'); input.read(data.data(), data.size()); data.resize(input.gcount());
    if (input.bad()) throw std::runtime_error("Could not read the complete JSON file");
    if (data.size() > limit) throw std::runtime_error("JSON file exceeds its size limit");
    std::vector<std::set<std::string>> keys;
    std::size_t nodes = 0;
    auto callback = [&](int depth, nlohmann::json::parse_event_t event, nlohmann::json& parsed) {
        if (depth > 64 || ++nodes > 500000) throw std::runtime_error("JSON structure exceeds its limit");
        if (event == nlohmann::json::parse_event_t::object_start) keys.emplace_back();
        if (event == nlohmann::json::parse_event_t::key && !keys.back().insert(parsed.get<std::string>()).second)
            throw std::runtime_error("Duplicate JSON field");
        if (event == nlohmann::json::parse_event_t::object_end) keys.pop_back();
        return true;
    };
    return nlohmann::json::parse(data, callback);
}
void WriteJson(const std::filesystem::path& file, const nlohmann::json& value, bool replace) {
    const auto text = value.dump(2) + "\n";
    if (text.size() > 1024 * 1024) throw std::runtime_error("Project exceeds the 1 MiB limit");
    const auto temporary = file.parent_path() / (file.filename().wstring() + L".tmp-" +
        std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64()));
    HANDLE handle = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot create the project file");
    DWORD written = 0;
    bool ok = WriteFile(handle, text.data(), static_cast<DWORD>(text.size()), &written, nullptr) &&
              written == text.size() && FlushFileBuffers(handle);
    CloseHandle(handle);
    if (ok) ok = MoveFileExW(temporary.c_str(), file.c_str(), MOVEFILE_WRITE_THROUGH | (replace ? MOVEFILE_REPLACE_EXISTING : 0));
    if (!ok) { DeleteFileW(temporary.c_str()); throw std::runtime_error("Could not save the complete project file"); }
}
static std::string Resolved(const std::string& name, const std::filesystem::path& parent) {
    if (name.empty() || name.size() > 32760 || name.find_first_of(std::string("\0\r\n", 3)) != std::string::npos)
        throw std::runtime_error("Invalid project path");
    auto path = std::filesystem::u8path(name);
    if ((path.has_root_name() || path.has_root_directory()) && !path.is_absolute())
        throw std::runtime_error("Project paths must be fully absolute or relative to the project file");
    return std::filesystem::absolute(path.is_absolute() ? path : parent / path).lexically_normal().u8string();
}

namespace {
using Json = nlohmann::json;
void Require(bool condition, const char* message) { if (!condition) throw std::runtime_error(message); }

std::size_t Characters(const std::string& value) {
    return static_cast<std::size_t>(std::count_if(value.begin(), value.end(), [](unsigned char c) { return (c & 0xc0) != 0x80; }));
}

void Text(const Json& value, std::size_t maximum, const char* message) {
    Require(value.is_string(), message);
    Require(Characters(value.get_ref<const std::string&>()) <= maximum, message);
}

bool Identifier(const std::string& value) {
    auto letter = [](char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_'; };
    return !value.empty() && letter(value[0]) && std::all_of(value.begin() + 1, value.end(), [&](char c) {
        return letter(c) || (c >= '0' && c <= '9') || c == '$';
    });
}

void Fields(const Json& value, const std::set<std::string>& allowed, const char* message) {
    Require(value.is_object(), message);
    for (const auto& item : value.items()) Require(allowed.count(item.key()) != 0, message);
}

void Integer(const Json& value, std::int64_t low, std::int64_t high, const char* message) {
    Require(value.is_number_integer(), message);
    if (value.is_number_unsigned()) Require(value.get<std::uint64_t>() <= static_cast<std::uint64_t>(high), message);
    auto number = value.get<std::int64_t>();
    Require(number >= low && number <= high, message);
}

void Number(const Json& value, double low, double high, const char* message) {
    Require(value.is_number(), message);
    auto number = value.get<double>();
    Require(std::isfinite(number) && number >= low && number <= high, message);
}

void Choice(const Json& value, const std::set<std::string>& allowed, const char* message) {
    Require(value.is_string() && allowed.count(value.get<std::string>()) != 0, message);
}

void Refinement(const Json& value) {
    Fields(value, {"mode", "model_id", "context_tokens", "model_memory_mib", "idle_seconds", "max_attempts", "budget_seconds", "request", "responses"}, "Unknown or malformed refinement setting");
    if (value.contains("mode")) Choice(value["mode"], {"off", "local", "replay"}, "Invalid refinement mode");
    if (value.contains("model_id")) Choice(value["model_id"], {"qwen3-4b-q4_k_m", "qwen3-4b-q5_k_m", "qwen3-14b-q4_k_m"}, "Unknown pinned model");
    if (value.contains("context_tokens") && !value["context_tokens"].is_null()) Integer(value["context_tokens"], 1024, 8192, "Invalid model context size");
    if (value.contains("model_memory_mib")) Integer(value["model_memory_mib"], 64, 12288, "Invalid model memory limit");
    if (value.contains("idle_seconds")) Integer(value["idle_seconds"], 1, 300, "Invalid model idle limit");
    if (value.contains("max_attempts")) Integer(value["max_attempts"], 1, 8, "Invalid refinement attempt count");
    if (value.contains("budget_seconds")) Number(value["budget_seconds"], 1, 300, "Invalid refinement time budget");
    if (value.contains("request")) Text(value["request"], 500, "Invalid layout request");
    if (value.contains("responses")) {
        const auto& responses = value["responses"];
        Require(responses.is_array() && responses.size() <= 8, "Invalid recorded response list");
        for (const auto& response : responses) Text(response, 65536, "Invalid recorded response");
        Require(responses.empty() || value.value("mode", std::string("off")) == "replay", "Recorded responses require replay mode");
    }
}

void Conversion(const Json& value) {
    Fields(value, {"spacing", "profile", "flatten", "tunnels", "refinement", "layout_strategy", "layout_timeout", "seed", "random_count"}, "Unknown or malformed conversion setting");
    if (value.contains("spacing")) {
        Integer(value["spacing"], 40, 400, "Invalid component spacing");
        Require(value["spacing"].get<int>() % 20 == 0, "Component spacing must be a multiple of 20");
    }
    if (value.contains("profile")) Choice(value["profile"], {"auto", "scalar-v1", "datapath-v1", "sequential-v1"}, "Invalid circuit profile");
    for (const auto* key : {"flatten", "tunnels"}) if (value.contains(key)) Require(value[key].is_boolean(), "Hierarchy and tunnel settings must be boolean");
    if (value.contains("layout_strategy")) Choice(value["layout_strategy"], {"auto", "reserved"}, "Invalid layout strategy");
    if (value.contains("layout_timeout")) Number(value["layout_timeout"], 1, 120, "Invalid layout time limit");
    if (value.contains("seed")) Require(value["seed"].is_number_integer(), "Layout seed must be an integer");
    if (value.contains("random_count")) Integer(value["random_count"], 0, 2048, "Invalid validation sample count");
    if (value.contains("refinement")) Refinement(value["refinement"]);
}

void Resources(const Json& value) {
    Fields(value, {"deadline_seconds", "memory_mib", "java_heap_mib", "process_memory_mib"}, "Unknown or malformed resource setting");
    if (value.contains("deadline_seconds")) Number(value["deadline_seconds"], 1, 3600, "Invalid job deadline");
    if (value.contains("memory_mib")) Integer(value["memory_mib"], 256, 65536, "Invalid job memory limit");
    if (value.contains("java_heap_mib")) Integer(value["java_heap_mib"], 64, 16384, "Invalid Java heap limit");
    if (value.contains("process_memory_mib") && !value["process_memory_mib"].is_null()) Integer(value["process_memory_mib"], 128, 65536, "Invalid process memory limit");
}

struct WindowsPathLess {
    bool operator()(const std::wstring& left, const std::wstring& right) const {
        return CompareStringOrdinal(left.data(), static_cast<int>(left.size()), right.data(), static_cast<int>(right.size()), TRUE) == CSTR_LESS_THAN;
    }
};
} // namespace

nlohmann::json LoadProject(const std::filesystem::path& file) {
    auto value = ReadJson(file);
    if (!value.is_object() || !value.contains("schema_version") || !value["schema_version"].is_number_integer()
        || value["schema_version"] != 1 || !value.contains("sources") ||
        !value["sources"].is_array() || value["sources"].empty() || value["sources"].size() > 64)
        throw std::runtime_error("Select a version 1 Verilog to Digital project with 1 to 64 sources");
    const std::set<std::string> fields{"schema_version", "sources", "include_dirs", "top", "defines", "parameters", "output", "conversion", "resources", "trace"};
    for (const auto& item : value.items()) if (!fields.count(item.key())) throw std::runtime_error("Unknown project field: " + item.key());
    for (const auto* key : {"sources", "include_dirs"}) if (value.contains(key)) {
        if (!value[key].is_array() || value[key].size() > 64) throw std::runtime_error("Invalid project path list");
        // Like the CLI: sources are unique after resolution; include entries only as written.
        const bool sources = std::string(key) == "sources";
        std::set<std::string> written;
        std::set<std::wstring, WindowsPathLess> seen;
        for (auto& name : value[key]) {
            Require(name.is_string(), "Project paths must be strings");
            if (!written.insert(name.get<std::string>()).second) throw std::runtime_error("Duplicate project path");
            name = Resolved(name.get<std::string>(), file.parent_path());
            if (sources && !seen.insert(std::filesystem::u8path(name.get<std::string>()).wstring()).second)
                throw std::runtime_error("Each source file must appear only once");
        }
    }
    for (const auto* key : {"output", "trace"}) if (value.contains(key)) {
        Require(value[key].is_string(), "Project paths must be strings");
        value[key] = Resolved(value[key].get<std::string>(), file.parent_path());
    }
    if (value.contains("top")) {
        Text(value["top"], 256, "Invalid top-module name");
        Require(Identifier(value["top"].get<std::string>()), "Top module must be an ordinary Verilog identifier");
    }
    for (const auto* key : {"defines", "parameters"}) if (value.contains(key)) {
        const auto& settings = value[key];
        Require(settings.is_object() && settings.size() <= 256, "Invalid macro or parameter object");
        for (const auto& item : settings.items()) {
            Require(Identifier(item.key()), "Invalid macro or parameter identifier");
            const bool define = std::string(key) == "defines";
            if (define || !item.value().is_number_integer()) Text(item.value(), 4096, "Invalid macro or parameter value");
            if (define) {
                const auto text = item.value().get<std::string>();
                Require(text.find_first_of(std::string("\0\r\n", 3)) == std::string::npos, "Macro values must fit on one line");
                Require((item.key() != "YOSYS" && item.key() != "SYNTHESIS") || text == "1", "The synthesis profile reserves YOSYS and SYNTHESIS as 1");
            }
        }
    }
    if (value.contains("conversion")) Conversion(value["conversion"]);
    if (value.contains("resources")) Resources(value["resources"]);
    return value;
}
nlohmann::json RelativeProject(nlohmann::json value, const std::filesystem::path& file) {
    auto relative = [&](nlohmann::json& name) {
        auto absolute = std::filesystem::u8path(name.get<std::string>());
        auto path = absolute.lexically_relative(file.parent_path());
        name = path.empty() ? absolute.generic_u8string() : path.generic_u8string();
    };
    for (const auto* key : {"sources", "include_dirs"}) if (value.contains(key)) for (auto& name : value[key]) relative(name);
    for (const auto* key : {"output", "trace"}) if (value.contains(key)) relative(value[key]);
    return value;
}
}
