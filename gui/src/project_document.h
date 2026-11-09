#pragma once
#include <filesystem>
#include <nlohmann/json.hpp>
#include <wx/string.h>

namespace v2d {
std::string Utf8(const wxString& value);
wxString Wide(const std::string& value);
std::filesystem::path Path(const wxString& value);
wxString Display(const std::filesystem::path& value);
nlohmann::json ReadJson(const std::filesystem::path& file, std::size_t limit = 1024 * 1024);
std::string Sha256File(const std::filesystem::path& file, std::size_t maxBytes = 8 * 1024 * 1024);
void WriteJson(const std::filesystem::path& file, const nlohmann::json& value, bool replace);
nlohmann::json LoadProject(const std::filesystem::path& file);
nlohmann::json RelativeProject(nlohmann::json value, const std::filesystem::path& file);
}
