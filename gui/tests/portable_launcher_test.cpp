#define V2D_PORTABLE_LAUNCHER_TEST
#include "../src/portable_launcher.cpp"
#include <fstream>
#include <iostream>

using namespace v2d::portable;
using namespace std::chrono_literals;

void Require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }

Json Read(const Path& path) {
    std::ifstream stream(path); Json result; stream >> result; return result;
}
void Write(const Path& path, const Json& value) {
    std::ofstream stream(path, std::ios::binary); stream << value.dump();
}
Json LaunchPlan(const Path& root, const Path& jobs) {
    return {{"schema_version",1},{"root",root.u8string()},
            {"gui",{{"executable",(root/L"gui/build/v2d-gui.exe").u8string()}}},
            {"environment",{{"V2D_ROOT",root.u8string()},{"V2D_JOB_ROOT",jobs.u8string()},{"PATH","C:\\Windows\\System32"}}}};
}

Handle FixtureProcess(const std::vector<std::wstring>& args, const Path& cwd, DWORD* pid = nullptr) {
    auto command = CommandLine(args); STARTUPINFOW startup{}; startup.cb = sizeof(startup);
    PROCESS_INFORMATION info{};
    Check(CreateProcessW(args.front().c_str(),command.data(),nullptr,nullptr,FALSE,CREATE_NO_WINDOW,nullptr,cwd.c_str(),&startup,&info),"Start test child");
    Handle thread(info.hThread); if (pid) *pid = info.dwProcessId; return Handle(info.hProcess);
}

void NotRunning(DWORD pid) {
    Handle process(OpenProcess(SYNCHRONIZE, FALSE, pid));
    if (process) Require(WaitForSingleObject(process.Get(),3000)==WAIT_OBJECT_0,"Owned test child survived cleanup");
    else Require(GetLastError()==ERROR_INVALID_PARAMETER,"Cannot inspect owned child completion");
}

int Bootstrap(const std::vector<std::wstring>& args) {
    auto at = std::find(args.begin(),args.end(),L"--root");
    Require(at!=args.end() && at+1!=args.end(),"Fixture root is missing");
    Path root(*(at+1)); const auto fixture = Read(root/L"fixture.json");
    Write(root/L"observed.json",{{"cwd",Path(std::filesystem::current_path()).u8string()},
                                {"root_env",Environment(root)[L"v2d_root"]==root.native()}});
    auto scenario = fixture.value("scenario", "valid");
    if (scenario=="leak" || scenario=="hang") {
        DWORD pid = 0; auto child=FixtureProcess({Executable().native(),L"--sleep"},root,&pid);
        Write(root/L"children.json",{{"parent",GetCurrentProcessId()},{"child",pid}});
        if (scenario=="hang") Sleep(60000);
    }
    if (scenario=="flood") { Output(GetStdHandle(STD_OUTPUT_HANDLE), std::string(100000,'x')); Sleep(60000); }
    if (scenario=="cli") {
        auto values=Json::array(); for(const auto& arg:args) values.push_back(Path(arg).u8string());
        Write(root/L"cli-observed.json",{{"argv",values}});
        Output(GetStdHandle(STD_OUTPUT_HANDLE),Json{{"argv",values}}.dump()+"\n"); return 0;
    }
    if (scenario=="large-cli") { Output(GetStdHandle(STD_OUTPUT_HANDLE),std::string(2*1024*1024,'x')); return 0; }
    Output(GetStdHandle(STD_OUTPUT_HANDLE),LaunchPlan(root,Wide(fixture.at("jobs").get<std::string>())).dump()+"\n");
    return 0;
}

int main() {
    int argc=0;auto** raw=CommandLineToArgvW(GetCommandLineW(),&argc);
    std::vector<std::wstring> args(raw,raw+argc);LocalFree(raw);
    SetErrorMode(GetErrorMode()|SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
    try {
        if(argc>1 && args[1]==L"--sleep") { Sleep(60000);return 0; }
        if(argc>1 && args[1]==L"-I") return Bootstrap(args);
        if(argc>2 && args[1]==L"--root") {
            Path root(args[2]);auto values=Json::array();for(const auto& arg:args)values.push_back(Path(arg).u8string());
            Write(root/L"gui.json",{{"pid",GetCurrentProcessId()},{"argv",values}});
            if(std::find(args.begin(),args.end(),L"--smoke-test")==args.end())Sleep(60000);
            return 0;
        }
        Require(argc==2,"Provide workspace root");
        auto repository=Absolute(args[1]);auto base=repository/L".runs/portable-launcher-tests"/(std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(GetTickCount64()));
        auto root=Absolute(base/L"portable-\u0131\u015f-\u03c0"); auto jobs=Absolute(base/L"jobs");
        std::filesystem::create_directories(root/L".tools/python-runtime");
        std::filesystem::create_directories(root/L"scripts");
        std::filesystem::create_directories(root/L"gui/build");
        std::filesystem::copy_file(Executable(),root/L".tools/python-runtime/python.exe");
        std::filesystem::copy_file(Executable(),root/L"gui/build/v2d-gui.exe");
        { std::ofstream script(root/L"scripts/portable_bootstrap.py");script<<"test executable interprets fixed invocation"; }
        auto fixture=[&](const char* scenario){Write(root/L"fixture.json",{{"scenario",scenario},{"jobs",jobs.u8string()}});};
        std::vector<std::wstring> command{(root/L".tools/python-runtime/python.exe").native(),L"-I",L"-X",L"utf8",(root/L"scripts/portable_bootstrap.py").native(),L"--root",root.native(),L"--prepare"};
        unsigned checks=0;
        auto plan=LaunchPlan(root,jobs);Require(ParsePlan(plan.dump(),root).executable==root/L"gui/build/v2d-gui.exe","Valid plan rejected");++checks;
        auto caseRoot = root.native();
        for (auto& c : caseRoot) if (c >= L'a' && c <= L'z') c -= L'a' - L'A';
        Require(SamePath(caseRoot,root),"Equivalent Windows path case was rejected");
        ParsePlan(plan.dump(),caseRoot);++checks;
        for(const auto& edit : std::vector<std::pair<std::string,Json>>{
            {"/schema_version",1.0},{"/schema_version",true},{"/root","C:\\another-root"},
            {"/gui/executable",(root/L"other.exe").u8string()},{"/environment/V2D_ROOT","C:\\wrong"},
            {"/environment/V2D_JOB_ROOT",(root/L"jobs").u8string()},{"/environment/V2D_JOB_ROOT","relative/jobs"},
            {"/environment/PYTHONPATH","C:\\injected"},{"/environment/PATH",std::string("x\0y",3)},
            {"/environment/V2D_NATIVE_TOOLS_ROOT","relative/native"}}) {
            auto bad=plan;bad[Json::json_pointer(edit.first)]=edit.second;bool rejected=false;
            try{ParsePlan(bad.dump(),root);}catch(...){rejected=true;}
            Require(rejected,"Malformed launch plan accepted");++checks;
        }
        for(const auto& text:std::vector<std::string>{"{\"schema_version\":1,\"schema_version\":1}",std::string("{\"x\":\"\xff\"}"),std::string(65537,' ')}) {
            bool rejected=false;try{ParsePlan(text,root);}catch(...){rejected=true;}Require(rejected,"Malformed JSON accepted");++checks;
        }
        for(const auto& arguments:std::vector<std::vector<std::wstring>>{{L"--root",L"C:\\injected"},{L"--cli"},{L"--project"},{L"--doctor",L"--smoke-test"},{L"--doctor",L"--project",L"x"}}) {
            bool rejected=false;try{ParseOptions(arguments);}catch(...){rejected=true;}Require(rejected,"Invalid launcher options accepted");++checks;
        }
        fixture("valid");auto capture=RunOwned(command,root,Environment(root),5s,true);
        Require(capture.exit_code==0 && capture.remaining_processes==0,"Owned bootstrap did not drain");
        ParsePlan(capture.output,root);Require(Read(root/L"observed.json")["cwd"]==root.u8string(),"Unicode cwd changed");++checks;
        fixture("cli");auto echo=command;echo.back()=L"--cli";
        for(const auto& arg:std::vector<std::wstring>{L"",L"with spaces\\",L"a\"b",L"\u0131\u015f\u03c0",L"& whoami",L"\\\\server\\share\\"})echo.push_back(arg);
        capture=RunOwned(echo,root,Environment(root),5s,false);auto echoed=Json::parse(capture.output)["argv"];
        Require(echoed.size()==echo.size(),"Argument count changed");
        for(std::size_t i=0;i<echo.size();++i)Require(echoed[i]==Path(echo[i]).u8string(),"Unicode/quoted argument changed");++checks;
        {
            auto original=std::filesystem::current_path();
            struct RestoreCwd { Path path; ~RestoreCwd() { std::error_code ignored; std::filesystem::current_path(path,ignored); } } restore{original};
            auto caller=Absolute(base/L"caller-\u0131\u015f-\u03c0");std::filesystem::create_directories(caller);
            std::filesystem::current_path(caller);
            auto cli=ParseOptions({L"--cli",L"inspect",L"--project",L"relative-\u03c0.json"});
            Require(Run(root,cli)==0,"CLI bootstrap failed from unrelated Unicode caller cwd");
            auto forwarded=Read(root/L"cli-observed.json")["argv"];
            Require(forwarded[7]=="--cwd" && forwarded[8]==caller.u8string(),"CLI caller cwd was not forwarded explicitly");
            Require(forwarded[9]=="--cli" && forwarded[10]=="inspect" && forwarded[11]=="--project"
                    && forwarded[12]==Path(L"relative-\u03c0.json").u8string(),"Relative CLI arguments were rewritten");
            Require(Read(root/L"observed.json")["cwd"]==root.u8string(),"Bootstrap did not retain its private package cwd");++checks;
        }
        fixture("large-cli");capture=RunOwned(echo,root,Environment(root),5s,false);
        Require(capture.output.size()==2*1024*1024 && capture.remaining_processes==0,"Output drain confused with process leak");++checks;
        for(const char* scenario: {"hang","leak","flood"}) {
            fixture(scenario);bool rejected=false;
            try{RunOwned(command,root,Environment(root),scenario==std::string("hang")?300ms:3s,true);}catch(...){rejected=true;}
            Require(rejected,"Bootstrap ownership failure accepted");
            if(std::string(scenario)!="flood") {auto pids=Read(root/L"children.json");NotRunning(pids["parent"]);NotRunning(pids["child"]);}
            ++checks;
        }
        fixture("valid");Options options;options.mode=Options::Mode::Smoke;options.project=root/L"project-\u03c0.json";
        Require(Run(root,options)==0,"End-to-end bootstrap/smoke handoff failed");
        auto observed=Read(root/L"gui.json");Require(observed["argv"][4]==options.project->u8string(),"Project argument changed at GUI handoff");++checks;
        options.mode=Options::Mode::Gui;
        auto owned=RunOwned(command,root,Environment(root),5s,true);
        auto independent=StartGui(ParsePlan(owned.output,root),root,options);
        Require(WaitForSingleObject(independent.Get(),200)==WAIT_TIMEOUT,"GUI inherited closed bootstrap ownership");
        Check(TerminateProcess(independent.Get(),0),"Stop exact-owned independent fixture GUI");
        Require(WaitForSingleObject(independent.Get(),3000)==WAIT_OBJECT_0,"Fixture GUI failed to stop");++checks;
        std::cout<<"PASS "<<checks<<" portable launcher checks\n";
        return 0;
    }catch(const std::exception& error){std::cerr<<"FAIL: "<<error.what()<<"\n";return 1;}
}
