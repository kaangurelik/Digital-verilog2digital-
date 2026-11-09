#define V2D_PORTABLE_LAUNCHER_TEST
#include "../src/portable_launcher.cpp"
#include <fstream>
#include <future>
#include <iostream>
#include <thread>

using namespace v2d::portable;
using namespace std::chrono_literals;

void Require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }

Json Read(const Path& path) {
    std::ifstream stream(path); Json result; stream >> result; return result;
}
Json ReadWhenReady(const Path& path, std::chrono::milliseconds timeout) {
    for (const auto deadline = Clock::now() + timeout;; Sleep(10)) {
        try { if (std::filesystem::exists(path)) return Read(path); } catch (const std::exception&) {}
        Require(Clock::now() < deadline, "Fixture process did not report its state");
    }
}

// The three pipes an MCP host creates; the launcher ends become its standard handles.
struct HostPipes {
    Handle input, inputHost, output, outputHost, error, errorHost;
    explicit HostPipes(bool inheritable = false) {
        SECURITY_ATTRIBUTES security{sizeof(security), nullptr, inheritable};
        for (auto [read, write] : {std::pair{&input, &inputHost}, std::pair{&outputHost, &output}, std::pair{&errorHost, &error}}) {
            HANDLE r = nullptr, w = nullptr; Check(CreatePipe(&r, &w, &security, 0), "Create host pipe");
            read->Reset(r); write->Reset(w);
        }
        for (auto* host : {&inputHost, &outputHost, &errorHost}) Check(SetHandleInformation(host->Get(), HANDLE_FLAG_INHERIT, 0), "Protect host pipe");
    }
    Streams Launcher() const { return {input.Get(), output.Get(), error.Get()}; }
};

Handle StartHost(const std::vector<std::wstring>& args, const Path& cwd, HostPipes& pipes) {
    auto command = CommandLine(args); STARTUPINFOW startup{}; startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = pipes.input.Get(); startup.hStdOutput = pipes.output.Get(); startup.hStdError = pipes.error.Get();
    PROCESS_INFORMATION info{};
    Check(CreateProcessW(args.front().c_str(),command.data(),nullptr,nullptr,TRUE,CREATE_NO_WINDOW,nullptr,cwd.c_str(),&startup,&info),"Start MCP host child");
    Handle thread(info.hThread);
    pipes.input.Reset(); pipes.output.Reset(); pipes.error.Reset();  // Like a host, keep only its own ends.
    return Handle(info.hProcess);
}

void WriteAll(HANDLE pipe, const std::string& bytes) {
    DWORD written = 0;
    Check(WriteFile(pipe, bytes.data(), static_cast<DWORD>(bytes.size()), &written, nullptr) && written == bytes.size(), "Write MCP host input");
}

// Reads up to `count` bytes or end of stream; stalling past the deadline fails the check.
std::string ReadFor(HANDLE pipe, std::size_t count, std::chrono::milliseconds timeout, bool* ended = nullptr) {
    std::string data; bool done = false;
    for (const auto deadline = Clock::now() + timeout; data.size() < count && !done; Sleep(5)) {
        ReadAvailable(pipe, data, count, done);
        Require(data.size() >= count || done || Clock::now() < deadline, "MCP stream did not deliver within its deadline");
    }
    if (ended) *ended = done;
    return data;
}

// Serve blocks for the server's lifetime; run it beside the host side of the test.
struct Background {
    std::shared_ptr<std::promise<int>> outcome = std::make_shared<std::promise<int>>();
    std::future<int> result = outcome->get_future();
    std::thread worker;
    template <class Body> explicit Background(Body body) : worker([outcome = outcome, body]() mutable {
        try { outcome->set_value(body()); } catch (...) { outcome->set_exception(std::current_exception()); } }) {}
    int Get(std::chrono::milliseconds timeout) {
        if (result.wait_for(timeout) != std::future_status::ready) { worker.detach(); throw std::runtime_error("Launcher operation exceeded its test deadline"); }
        worker.join(); return result.get();
    }
    ~Background() { if (worker.joinable()) worker.detach(); }
};

std::string Failure(Background& server, std::chrono::milliseconds timeout) {
    try { server.Get(timeout); } catch (const std::runtime_error& error) { return error.what(); }
    return {};
}

std::vector<std::wstring> McpCommand(const Path& root, const Path& caller, const std::vector<std::wstring>& extra) {
    std::vector<std::wstring> command{(root/L".tools/python-runtime/python.exe").native(),L"-I",L"-X",L"utf8",(root/L"scripts/portable_bootstrap.py").native(),
                                      L"--root",root.native(),L"--cwd",caller.native(),L"--mcp"};
    command.insert(command.end(), extra.begin(), extra.end());
    return command;
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

Path Junction(const Path& alias, const Path& target) {
    wchar_t system[MAX_PATH]; Require(GetSystemDirectoryW(system,MAX_PATH)!=0,"Cannot locate cmd.exe");
    std::wstring command=L"\""+(Path(system)/L"cmd.exe").native()+L"\" /d /s /c \"mklink /J \""+alias.native()+L"\" \""+target.native()+L"\"\"";
    STARTUPINFOW startup{}; startup.cb=sizeof(startup); PROCESS_INFORMATION info{};
    Check(CreateProcessW(nullptr,command.data(),nullptr,nullptr,FALSE,CREATE_NO_WINDOW,nullptr,nullptr,&startup,&info),"Start junction fixture");
    Handle process(info.hProcess),thread(info.hThread);
    Require(WaitForSingleObject(process.Get(),10000)==WAIT_OBJECT_0 && std::filesystem::is_directory(alias/L"scripts"),"Cannot create the junction fixture");
    return alias;
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
    if (scenario=="mcp-echo") {
        // A stand-in `v2d mcp`: log to stderr, echo protocol bytes until the host closes input.
        auto values=Json::array(); for(const auto& arg:args) values.push_back(Path(arg).u8string());
        Write(root/L"mcp-observed.json",{{"argv",values}});
        Output(GetStdHandle(STD_ERROR_HANDLE),"diagnostic-\xcf\x80\n");
        char buffer[4096]; DWORD count=0;
        while(ReadFile(GetStdHandle(STD_INPUT_HANDLE),buffer,sizeof(buffer),&count,nullptr) && count) {
            DWORD written=0;
            if(!WriteFile(GetStdHandle(STD_OUTPUT_HANDLE),buffer,count,&written,nullptr) || written!=count) return 3;
        }
        return 7;
    }
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
        // Acts as VerilogToDigital.exe for a host that owns this process's standard handles.
        if(argc>2 && args[1]==L"--launch") return Run(Absolute(args[2]),ParseOptions(std::vector<std::wstring>(args.begin()+3,args.end())));
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
        {
            // Python reports a junction, substituted or mapped drive by its final path.
            auto alias=Junction(Absolute(base/L"junction-\u03c0"),root);
            struct RemoveJunction { Path path; ~RemoveJunction(){ RemoveDirectoryW(path.c_str()); } } cleanup{alias};
            Require(ParsePlan(plan.dump(),alias).executable==root/L"gui/build/v2d-gui.exe" && SamePath(alias/L"scripts",root/L"scripts"),
                    "Launcher rejected its own package reached through a junction");
            auto other=Absolute(base/L"other"); std::filesystem::create_directories(other/L"scripts");
            Require(!SamePath(other,root) && !SamePath(other/L"scripts",alias/L"scripts"),"Distinct directories compared equal");++checks;
        }
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
        for(const auto& arguments:std::vector<std::vector<std::wstring>>{{L"--root",L"C:\\injected"},{L"--cli"},{L"--project"},{L"--doctor",L"--smoke-test"},{L"--doctor",L"--project",L"x"},
                                                                         {L"--doctor",L"--mcp"},{L"--project",L"x",L"--mcp"},{L"--smoke-test",L"--mcp",L"--allow-dir"}}) {
            bool rejected=false;try{ParseOptions(arguments);}catch(...){rejected=true;}Require(rejected,"Invalid launcher options accepted");++checks;
        }
        {
            // `v2d mcp` owns its arguments; a bare --mcp starts it with its defaults.
            auto mcp=ParseOptions({L"--mcp"});Require(mcp.mode==Options::Mode::Mcp && mcp.cli.empty(),"Bare --mcp was rejected");
            const std::vector<std::wstring> forwarded{L"--allow-dir",L"C:\\with spaces\\",L"--deadline",L"60",L"--cli"};
            mcp=ParseOptions({L"--mcp",L"--allow-dir",L"C:\\with spaces\\",L"--deadline",L"60",L"--cli"});
            Require(mcp.mode==Options::Mode::Mcp && mcp.cli==forwarded,"MCP arguments changed while parsing");++checks;
        }
        {
            // The desktop receives this environment; internal runtime overrides must not reach it.
            const std::vector<const wchar_t*> names{L"V2D_JOB_TMP",L"V2D_JAVA_HEAP_BYTES",L"V2D_JAVA",L"JAVA_HOME",L"TCL_LIBRARY",
                                                    L"VIRTUAL_ENV",L"PYTHONOPTIMIZE",L"V2D_JOB_ROOT",L"V2D_NATIVE_TOOLS_ROOT"};
            std::vector<std::pair<std::wstring,std::optional<std::wstring>>> previous;
            for(const auto* name:names){
                wchar_t buffer[32768]; auto size=GetEnvironmentVariableW(name,buffer,32768);
                previous.emplace_back(name,size?std::optional<std::wstring>(std::wstring(buffer,size)):std::nullopt);
                SetEnvironmentVariableW(name,L"C:\\inherited");
            }
            auto sanitized=Environment(root);
            for(const auto& item:previous)SetEnvironmentVariableW(item.first.c_str(),item.second?item.second->c_str():nullptr);
            for(const auto* name:{L"v2d_job_tmp",L"v2d_java_heap_bytes",L"v2d_java",L"java_home",L"tcl_library",L"virtual_env",L"pythonoptimize"})
                Require(!sanitized.count(name),"Inherited runtime override reached the bootstrap or desktop");
            Require(sanitized[L"v2d_job_root"]==L"C:\\inherited" && sanitized[L"v2d_native_tools_root"]==L"C:\\inherited"
                    && sanitized[L"v2d_root"]==root.native() && sanitized[L"pythonutf8"]==L"1","Supported launcher settings changed");++checks;
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
        {
            // Without host protocol streams nothing starts: no bootstrap, no message box.
            fixture("mcp-echo");std::filesystem::remove(root/L"observed.json");
            for(const auto& streams:{Streams{nullptr,nullptr,nullptr},Streams{INVALID_HANDLE_VALUE,INVALID_HANDLE_VALUE,nullptr}}) {
                bool rejected=false;try{Serve(McpCommand(root,root,{}),root,Environment(root),streams);}catch(const std::runtime_error&){rejected=true;}
                Require(rejected,"MCP mode started without host protocol streams");
            }
            Require(!std::filesystem::exists(root/L"observed.json"),"MCP bootstrap started without host protocol streams");++checks;
        }
        {
            // Each request is answered while input stays open, byte for byte, with a NUL log sink.
            fixture("mcp-echo");HostPipes pipes;
            Background server([&]{return Serve(McpCommand(root,root,{}),root,Environment(root),{pipes.input.Get(),pipes.output.Get(),nullptr});});
            const std::string request="{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}\n";
            WriteAll(pipes.inputHost.Get(),request);
            Require(ReadFor(pipes.outputHost.Get(),request.size(),10s)==request,"MCP request was not passed through live");
            std::string binary;for(int round=0;round<3;++round)for(int value=0;value<256;++value)binary.push_back(static_cast<char>(value));
            binary+="\r\n\x1a\r\r\n\xef\xbb\xbf";
            WriteAll(pipes.inputHost.Get(),binary);
            Require(ReadFor(pipes.outputHost.Get(),binary.size(),10s)==binary,"MCP bytes changed in transit");
            pipes.inputHost.Reset();
            Require(server.Get(10s)==7,"MCP server exit code was not propagated");++checks;
        }
        {
            // A server that ignores its closed input is stopped with its descendants after the grace period.
            fixture("hang");std::filesystem::remove(root/L"children.json");HostPipes pipes;
            Background server([&]{return Serve(McpCommand(root,root,{}),root,Environment(root),pipes.Launcher(),300ms);});
            auto pids=ReadWhenReady(root/L"children.json",10s);
            pipes.inputHost.Reset();
            Require(Failure(server,15s).find("after its input closed")!=std::string::npos,"MCP server outlived its closed input");
            NotRunning(pids["parent"]);NotRunning(pids["child"]);++checks;
        }
        {
            fixture("leak");std::filesystem::remove(root/L"children.json");HostPipes pipes;
            Background server([&]{return Serve(McpCommand(root,root,{}),root,Environment(root),pipes.Launcher());});
            Require(Failure(server,15s).find("live descendant")!=std::string::npos,"MCP server exit left a live descendant");
            NotRunning(ReadWhenReady(root/L"children.json",1s)["child"]);++checks;
        }
        {
            // A real host process: Run() forwards caller cwd and arguments, stdout carries only the
            // server's bytes, stderr carries its log, and the server's exit code is returned.
            fixture("mcp-echo");HostPipes pipes(true);
            auto caller=Absolute(base/L"mcp-host-\u0131\u015f-\u03c0");std::filesystem::create_directories(caller);
            const std::vector<std::wstring> extra{L"--allow-dir",L"relative dir \u03c0",L"--deadline",L"60",L"a\"b\\"};
            std::vector<std::wstring> host{Executable().native(),L"--launch",root.native(),L"--mcp"};host.insert(host.end(),extra.begin(),extra.end());
            auto launcher=StartHost(host,caller,pipes);
            struct StopHost { HANDLE process; ~StopHost(){ if(WaitForSingleObject(process,0)==WAIT_TIMEOUT) TerminateProcess(process,1); } } guard{launcher.Get()};
            const std::string request="{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"name\":\"\xcf\x80\"}}\n";
            WriteAll(pipes.inputHost.Get(),request);
            Require(ReadFor(pipes.outputHost.Get(),request.size(),20s)==request,"Launcher did not pass the protocol stream through live");
            pipes.inputHost.Reset();
            Require(WaitForSingleObject(launcher.Get(),10000)==WAIT_OBJECT_0,"Launcher did not exit after its host closed input");
            DWORD code=0;Check(GetExitCodeProcess(launcher.Get(),&code),"Read launcher exit code");Require(code==7,"Launcher changed the MCP server exit code");
            bool ended=false;Require(ReadFor(pipes.outputHost.Get(),1,5s,&ended).empty() && ended,"Launcher or bootstrap wrote non-protocol bytes");
            Require(ReadFor(pipes.errorHost.Get(),65536,5s,&ended).find("diagnostic-\xcf\x80\n")!=std::string::npos && ended,"Server log did not reach the host's stderr");
            auto argv=Read(root/L"mcp-observed.json")["argv"];
            Require(argv.size()==10+extra.size() && argv[5]=="--root" && argv[6]==root.u8string() && argv[7]=="--cwd"
                    && argv[8]==caller.u8string() && argv[9]=="--mcp","MCP bootstrap invocation changed");
            for(std::size_t i=0;i<extra.size();++i)Require(argv[10+i]==Path(extra[i]).u8string(),"MCP argument changed");
            Require(Read(root/L"observed.json")["cwd"]==root.u8string(),"MCP bootstrap left its private package cwd");++checks;
        }
        {
            // A host that terminates the launcher also ends the bootstrap, server and descendants.
            fixture("hang");std::filesystem::remove(root/L"children.json");HostPipes pipes(true);
            auto launcher=StartHost({Executable().native(),L"--launch",root.native(),L"--mcp"},root,pipes);
            auto pids=ReadWhenReady(root/L"children.json",10s);
            Check(TerminateProcess(launcher.Get(),1),"Terminate MCP launcher");
            Require(WaitForSingleObject(launcher.Get(),5000)==WAIT_OBJECT_0,"MCP launcher did not stop");
            NotRunning(pids["parent"]);NotRunning(pids["child"]);++checks;
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
