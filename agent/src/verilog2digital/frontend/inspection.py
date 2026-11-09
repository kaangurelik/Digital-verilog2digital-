"""Inspect Verilog-2005 syntax and dependencies with pyslang before synthesis.

Yosys supplies electrical connectivity; this pass rejects unsupported syntax
before optimization can erase it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import pyslang
from pyslang import analysis, ast, parsing, syntax

from .errors import FrontendError


# Allow only syntax supported by the Yosys/Icarus configuration.
_SUPPORTED_NODES = frozenset("""
CompilationUnit ModuleDeclaration ModuleHeader AnsiPortList NonAnsiPortList
ImplicitAnsiPort ExplicitAnsiPort ImplicitNonAnsiPort ExplicitNonAnsiPort
EmptyNonAnsiPort VariablePortHeader NetPortHeader PortDeclaration PortReference
PortConcatenation Declarator ImplicitType RegType IntegerType VariableDimension
RangeDimensionSpecifier SimpleRangeSelect AscendingRangeSelect DescendingRangeSelect
DataDeclaration NetDeclaration ParameterDeclaration ParameterDeclarationStatement
ParameterPortList ParameterValueAssignment NamedParamAssignment OrderedParamAssignment
EqualsValueClause IdentifierName IdentifierSelectName EmptyIdentifierName
ElementSelect ElementSelectExpression BitSelect HierarchyInstantiation HierarchicalInstance
InstanceName NamedPortConnection OrderedPortConnection EmptyPortConnection
PrimitiveInstantiation ContinuousAssign AlwaysBlock TimingControlStatement
ImplicitEventControl EventControl EventControlWithExpression ParenthesizedEventExpression
SignalEventExpression BinaryEventExpression SequentialBlockStatement EmptyStatement
ExpressionStatement ConditionalStatement ConditionalPredicate ConditionalPattern
ElseClause CaseStatement StandardCaseItem DefaultCaseItem ForLoopStatement LoopStatement
FunctionDeclaration TaskDeclaration FunctionPrototype FunctionPortList FunctionPort
ArgumentList OrderedArgument EmptyArgument InvocationExpression SystemName
GenerateRegion GenerateBlock GenvarDeclaration LoopGenerate IfGenerate CaseGenerate
NamedBlockClause NamedLabel EmptyMember AttributeInstance AttributeSpec
SimplePropertyExpr SimpleSequenceExpr ParenthesizedExpression IntegerLiteralExpression
IntegerVectorExpression ConcatenationExpression MultipleConcatenationExpression
AssignmentExpression NonblockingAssignmentExpression AddExpression SubtractExpression
MultiplyExpression DivideExpression ModExpression PowerExpression BinaryAndExpression
BinaryOrExpression BinaryXorExpression BinaryXnorExpression LogicalAndExpression
LogicalOrExpression LogicalShiftLeftExpression LogicalShiftRightExpression
ArithmeticShiftLeftExpression ArithmeticShiftRightExpression EqualityExpression
InequalityExpression CaseEqualityExpression CaseInequalityExpression LessThanExpression
LessThanEqualExpression GreaterThanExpression GreaterThanEqualExpression
ConditionalExpression UnaryPlusExpression UnaryMinusExpression UnaryBitwiseNotExpression
UnaryLogicalNotExpression UnaryBitwiseAndExpression UnaryBitwiseNandExpression
UnaryBitwiseOrExpression UnaryBitwiseNorExpression UnaryBitwiseXorExpression
UnaryBitwiseXnorExpression
""".split())

_TIMING_NODES = frozenset("""
DelayControl Delay3 TimingControlExpression CycleDelay OneStepDelay RepeatedEventControl
WaitStatement WaitForkStatement WaitOrderStatement ForeverStatement ParallelBlockStatement
BlockingEventTriggerStatement NonblockingEventTriggerStatement SpecifyBlock SystemTimingCheck
ProceduralAssignStatement ProceduralDeassignStatement ProceduralForceStatement
ProceduralReleaseStatement DisableStatement DisableForkStatement
""".split())
_REAL_NODES = {"RealType", "RealTimeType", "ShortRealType", "RealLiteralExpression", "TimeLiteralExpression"}
_PRIMITIVES = {"AndKeyword", "NandKeyword", "OrKeyword", "NorKeyword", "XorKeyword", "XnorKeyword", "NotKeyword", "BufKeyword"}
_SYSTEM_FUNCTIONS = {"$clog2", "$signed", "$unsigned"}
_ALLOWED_DIRECTIVES = frozenset("""
DefineDirective UndefDirective UndefineAllDirective IfDefDirective IfNDefDirective
ElsIfDirective ElseDirective EndIfDirective IncludeDirective DefaultNetTypeDirective
TimeScaleDirective ResetAllDirective MacroUsage
""".split())


def _location(manager: pyslang.SourceManager, location: pyslang.SourceLocation) -> dict:
    if location == pyslang.SourceLocation.NoLocation:
        return {}
    expanded = manager.getFullyExpandedLoc(location)
    displayed = str(manager.getFileName(expanded))
    path = str(manager.getFullPath(expanded.buffer))
    if displayed.startswith("<") or path.startswith("<"):
        return {}
    return {"path": path, "line": manager.getLineNumber(expanded),
            "column": manager.getColumnNumber(expanded)}


def _failure(manager: pyslang.SourceManager, node, code: str, message: str) -> None:
    location = node.location if isinstance(node, parsing.Token) else node.sourceRange.start
    raise FrontendError(code, message, **_location(manager, location))


def _walk_nodes(root: syntax.SyntaxNode) -> Iterable[syntax.SyntaxNode]:
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, syntax.SyntaxNode):
            yield node
            stack.extend(reversed(list(node)))


def _inspect_tree(tree: syntax.SyntaxTree, manager: pyslang.SourceManager) -> dict[str, syntax.SyntaxNode]:
    modules: dict[str, syntax.SyntaxNode] = {}
    # Track ancestors explicitly: Python wrappers need not preserve object identity.
    stack = [(tree.root, ())]
    while stack:
        node, ancestors = stack.pop()
        if isinstance(node, parsing.Token):
            if node.kind.name == "InOutKeyword":
                _failure(manager, node, "UNSUPPORTED_INOUT", "Bidirectional ports are outside the binary single-driver profile")
            if node.kind.name in {"CaseXKeyword", "CaseZKeyword"}:
                _failure(manager, node, "UNSUPPORTED_FOUR_STATE", "casex/casez wildcard semantics are not supported; use ordinary case")
            if isinstance(node.value, pyslang.SVInt) and node.value.hasUnknown:
                _failure(manager, node, "UNSUPPORTED_FOUR_STATE", "X, Z and ? digits in RTL literals are outside the binary profile, including unused expressions")
            for trivia in node.trivia:
                if trivia.kind.name != "Directive":
                    continue
                directive = trivia.syntax()
                if directive is not None and directive.kind.name not in _ALLOWED_DIRECTIVES:
                    _failure(manager, directive, "UNSUPPORTED_DIRECTIVE", f"Directive {directive.kind.name} is outside the source profile")
            continue
        if not isinstance(node, syntax.SyntaxNode):
            continue
        kind = node.kind.name
        if kind in {"InitialBlock", "FinalBlock"}:
            _failure(manager, node, "UNSUPPORTED_INITIALIZATION", "initial/final blocks are not supported, even when their results are unused")
        if kind in _TIMING_NODES:
            _failure(manager, node, "UNSUPPORTED_TIMING", f"{kind} cannot be represented by this synthesizable circuit profile")
        if kind in _REAL_NODES:
            _failure(manager, node, "UNSUPPORTED_REAL", "Real-valued and physical-time types or literals are not supported")
        if kind == "TimingControlStatement":
            if not ancestors or ancestors[-1].kind.name != "AlwaysBlock":
                _failure(manager, node, "UNSUPPORTED_TIMING", "Event controls are allowed only as the outer sensitivity control of always")
        if kind == "AlwaysBlock" and node.statement.kind.name != "TimingControlStatement":
            _failure(manager, node, "UNSUPPORTED_TIMING", "always requires an outer @* or explicit sensitivity control")
        if kind == "SignalEventExpression" and node.edge.kind.name == "NegEdgeKeyword":
            _failure(manager, node, "UNSUPPORTED_CLOCK", "Only rising-edge clocks are supported in the initial sequential profile")
        if kind == "EventControlWithExpression":
            events = [item for item in _walk_nodes(node) if item.kind.name == "SignalEventExpression"]
            edges = [item for item in events if item.edge.kind.name in {"PosEdgeKeyword", "NegEdgeKeyword", "EdgeKeyword"}]
            if edges and (len(events) != 1 or len(edges) != 1):
                _failure(manager, node, "UNSUPPORTED_CLOCK", "Multiple-edge and asynchronous-reset sensitivity lists are not supported")
        if kind == "Declarator":
            if len(node.dimensions):
                _failure(manager, node, "UNSUPPORTED_MEMORY", "Unpacked arrays and memories are outside the initial profile")
            if node.initializer is not None:
                parent = ancestors[-1].kind.name if ancestors else ""
                if parent not in {"ParameterDeclaration", "NetDeclaration"}:
                    _failure(manager, node, "UNSUPPORTED_INITIALIZATION", "Variable/register declaration initialization requires an explicit supported reset contract")
        if kind == "NetDeclaration" and node.netType.kind.name not in {"WireKeyword", "Supply0Keyword", "Supply1Keyword"}:
            _failure(manager, node, "UNSUPPORTED_NET_TYPE", "Only ordinary wires and binary supply constants are supported")
        if kind == "PrimitiveInstantiation" and node.type.kind.name not in _PRIMITIVES:
            _failure(manager, node, "UNSUPPORTED_PRIMITIVE", "Switching, tri-state and pull-device primitives are not supported")
        if kind == "SystemName" and node.systemIdentifier.valueText not in _SYSTEM_FUNCTIONS:
            _failure(manager, node, "UNSUPPORTED_SYSTEM_CALL", f"System call {node.systemIdentifier.valueText} is not in the supported constant/cast function inventory")
        if kind == "AttributeSpec":
            attribute = node.getFirstToken().valueText
            code = "UNSUPPORTED_BLACKBOX" if attribute in {"blackbox", "whitebox", "lib_whitebox"} else "UNSUPPORTED_ATTRIBUTE"
            _failure(manager, node, code, f"Source attribute {attribute} is not supported; attributes must not alter synthesis semantics silently")
        if kind not in _SUPPORTED_NODES:
            _failure(manager, node, "UNSUPPORTED_SYSTEMVERILOG", f"Syntax {kind} is outside the supported synthesizable Verilog-2005 subset")
        if kind == "ModuleDeclaration":
            if any(parent.kind.name == "ModuleDeclaration" for parent in ancestors):
                _failure(manager, node, "UNSUPPORTED_SYSTEMVERILOG", "Nested module declarations are not supported")
            name = node.header.name.valueText
            if name in modules:
                _failure(manager, node, "DUPLICATE_MODULE", f"Module {name} is declared more than once")
            modules[name] = node
        stack.extend((child, (*ancestors, node)) for child in reversed(list(node)))
    return modules


def _parameter_names(module: syntax.SyntaxNode) -> set[str]:
    roots = []
    if module.header.parameters is not None:
        roots.append(module.header.parameters)
    roots.extend(member for member in module.members if isinstance(member, syntax.SyntaxNode)
                 and member.kind.name == "ParameterDeclarationStatement")
    result = set()
    for root in roots:
        for node in _walk_nodes(root):
            if node.kind.name == "ParameterDeclaration" and node.keyword.kind.name == "ParameterKeyword":
                result.update(item.name.valueText for item in node.declarators if isinstance(item, syntax.SyntaxNode)
                              and item.kind.name == "Declarator")
    return result


def _diagnostics(compilation: ast.Compilation, manager: pyslang.SourceManager) -> list[dict]:
    engine = pyslang.DiagnosticEngine(manager)
    result = []
    for diagnostic in compilation.getAllDiagnostics():
        severity = engine.getSeverity(diagnostic.code, diagnostic.location).name.lower()
        message = engine.formatMessage(diagnostic)
        location = _location(manager, diagnostic.location)
        if severity in {"error", "fatal"}:
            raise FrontendError("SLANG_ERROR", message, **location)
        if severity not in {"ignored", "note"}:
            result.append({"code": str(diagnostic.code), "message": message, "severity": severity, **location})
    return result


def _signal_events(control):
    if isinstance(control, ast.EventListControl):
        for event in control.events:
            yield from _signal_events(event)
    elif isinstance(control, ast.SignalEventControl):
        yield control


def _plain_event_expression(expression) -> bool:
    if isinstance(expression, ast.NamedValueExpression):
        return True
    if isinstance(expression, ast.ElementSelectExpression):
        return expression.selector.constant is not None and _plain_event_expression(expression.value)
    if isinstance(expression, ast.RangeSelectExpression):
        return (expression.left.constant is not None and expression.right.constant is not None
                and _plain_event_expression(expression.value))
    return False


def _covered(required: tuple[int, int], available: list[tuple[int, int]]) -> bool:
    cursor, last = required
    for first, end in sorted(available):
        if first > cursor:
            break
        cursor = max(cursor, end + 1)
        if cursor > last:
            return True
    return False


def _check_sensitivity(compilation: ast.Compilation, source_manager: pyslang.SourceManager) -> None:
    """Compare native read intervals with actual triggers before synthesis drops them."""
    # Analysis requires final elaboration (getAllDiagnostics above) and freezing.
    compilation.freeze()
    options = analysis.AnalysisOptions()
    options.maxCaseAnalysisSteps = 10000
    options.maxLoopAnalysisSteps = 10000
    manager = analysis.AnalysisManager(options)
    procedures = []

    def symbol_key(symbol):
        # Keep only Python values, including for symbols owned by the compilation.
        return (str(symbol.hierarchicalPath), tuple(sorted(_location(source_manager, symbol.location).items())))

    def read_snapshot(read):
        symbol = read.symbol
        return {"key": symbol_key(symbol), "name": str(symbol.hierarchicalPath),
                "bits": tuple(int(bit) for bit in read.bitRange),
                "constant": isinstance(symbol, ast.ParameterSymbol)}

    def snapshot(procedure):
        # The callback borrows native objects that analysis may move or free.
        # Copy needed values before returning; do not retain their wrappers.
        symbol = procedure.analyzedSymbol
        subroutine = isinstance(symbol, ast.SubroutineSymbol)
        always = (isinstance(symbol, ast.ProceduralBlockSymbol)
                  and symbol.procedureKind == ast.ProceduralBlockKind.Always)
        if not subroutine and not always:
            return
        item = {"key": symbol_key(symbol), "prefix": str(symbol.hierarchicalPath) + ".",
                "subroutine": subroutine, "always": always,
                "location": _location(source_manager, symbol.location),
                "reads": [read_snapshot(read) for read in procedure.readSet],
                "calls": [(symbol_key(call.subroutine), _location(source_manager, call.sourceRange.start))
                          for call in procedure.callExpressions if not call.isSystemCall]}
        if always:
            sensitivity = procedure.sensitivityList
            events = list(_signal_events(sensitivity.timingControl)) if sensitivity.timingControl is not None else []
            item.update({"sensitivity_kind": sensitivity.kind.name,
                         "clocked": any(event.edge != ast.EdgeKind.None_ for event in events),
                         "plain_events": sensitivity.timingControl is None or (
                             bool(events) and all(_plain_event_expression(event.expr) for event in events)),
                         "sensitivity_reads": [read_snapshot(read) for read in sensitivity.reads]})
        procedures.append(item)

    manager.addProcListener(snapshot)
    manager.analyze(compilation)
    subroutines = {procedure["key"]: procedure for procedure in procedures if procedure["subroutine"]}

    def called_global_reads(procedure, visited):
        reads = []
        for callee_key, call_location in procedure["calls"]:
            if callee_key in visited:
                continue
            visited.add(callee_key)
            callee = subroutines.get(callee_key)
            if callee is None:
                raise FrontendError("UNSUPPORTED_SENSITIVITY", "Unable to inspect a called subroutine's signal dependencies", **call_location)
            reads.extend(read for read in callee["reads"] if not read["name"].startswith(callee["prefix"]))
            reads.extend(called_global_reads(callee, visited))
        return reads

    for procedure in procedures:
        if not procedure["always"]:
            continue
        location = procedure["location"]
        if procedure["sensitivity_kind"] not in {"Explicit", "Implicit"}:
            raise FrontendError("UNSUPPORTED_SENSITIVITY", "always requires a static sensitivity list", **location)
        if procedure["clocked"]:
            continue  # Sequential input/reset/clock semantics are checked by the cell profile.
        if not procedure["plain_events"]:
            raise FrontendError("UNSUPPORTED_SENSITIVITY", "Combinational event triggers must be plain signals or constant slices; use @* for expressions", **location)
        available = {}
        for read in procedure["sensitivity_reads"]:
            available.setdefault(read["key"], []).append(read["bits"])
        required = procedure["reads"] + called_global_reads(procedure, set())
        for read in required:
            if read["constant"]:
                continue
            if not _covered(read["bits"], available.get(read["key"], [])):
                raise FrontendError("INCOMPLETE_SENSITIVITY",
                                    f"Sensitivity list does not cover reads of {read['name']}. "
                                    "Use @* for direct expressions, and pass function dependencies explicitly as arguments", **location)


def inspect_sources(sources: list[Path], *, top: str | None = None,
                    include_dirs: list[Path] = (), defines: dict[str, str] | None = None,
                    parameters: dict[str, str | int] | None = None, discover: bool = False) -> dict:
    """Inspect ordered sources as one Verilog-2005 compilation unit.

    Check unused modules and dead generate branches too. Return warnings and
    source-located errors; Yosys checks connectivity and cell support separately.
    """
    if not sources:
        raise FrontendError("NO_SOURCES", "Add at least one Verilog source file")
    paths = [Path(source).resolve() for source in sources]
    include_paths = [Path(directory).resolve() for directory in include_dirs]
    if os.name == "nt":
        for path in [*paths, *include_paths]:
            if not str(path).isascii():
                raise FrontendError("UNSUPPORTED_PATH",
                                    "The current Windows HDL tools require ASCII project and include paths. "
                                    "Use the CLI, desktop or conversion.convert() to stage Unicode paths, or move the project and includes to an ASCII-only path.",
                                    path=str(path))
    for path in paths:
        if not path.is_file():
            raise FrontendError("MISSING_SOURCE", f"Source file does not exist: {path}", path=str(path))
    defines = dict(defines or {})
    parameters = dict(parameters or {})
    manager = pyslang.SourceManager()
    for directory in include_paths:
        manager.addUserDirectories(str(directory))
    preprocessing = parsing.PreprocessorOptions()
    preprocessing.languageVersion = pyslang.LanguageVersion.v1364_2005
    preprocessing.predefines = [f"{name}={value}" for name, value in defines.items()]
    preprocessing.predefineSource = "<v2d-defines>"
    preprocessing.maxIncludeDepth = 64
    parser = parsing.ParserOptions()
    parser.languageVersion = pyslang.LanguageVersion.v1364_2005
    parser.maxRecursionDepth = 256
    options = ast.CompilationOptions()
    options.languageVersion = pyslang.LanguageVersion.v1364_2005
    options.topModules = {top} if top is not None else set()
    options.paramOverrides = [f"{name}={value}" for name, value in parameters.items()]
    options.maxInstanceDepth = 128
    options.maxGenerateSteps = 10000
    options.maxConstexprSteps = 100000
    options.maxConstantSize = 65536
    options.maxInstanceArray = 1024
    options.errorLimit = 20
    bag = pyslang.Bag([preprocessing, parser, options])
    try:
        tree = syntax.SyntaxTree.fromFiles([str(path) for path in paths], manager, bag)
    except OSError as error:
        raise FrontendError("SOURCE_READ_FAILED", str(error)) from error

    # Parsing errors take precedence over partial-tree feature guesses.
    engine = pyslang.DiagnosticEngine(manager)
    for diagnostic in tree.diagnostics:
        if engine.getSeverity(diagnostic.code, diagnostic.location).name.lower() in {"error", "fatal"}:
            raise FrontendError("SLANG_ERROR", engine.formatMessage(diagnostic), **_location(manager, diagnostic.location))
    modules = _inspect_tree(tree, manager)
    if top is not None and top not in modules:
        raise FrontendError("UNKNOWN_TOP", f"Top module {top} was not declared; available modules: {', '.join(sorted(modules))}")
    compilation = ast.Compilation(bag)
    compilation.addSyntaxTree(tree)
    root = compilation.getRoot()
    tops = sorted(instance.name for instance in root.topInstances)
    if len(tops) > 1 and not discover:
        raise FrontendError("AMBIGUOUS_TOP", f"Select one top module: {', '.join(tops)}")
    if not tops:
        _diagnostics(compilation, manager)
        raise FrontendError("NO_TOP", "No elaboratable top module was found")
    selected = tops[0] if len(tops) == 1 else None
    unknown_parameters = set(parameters) - _parameter_names(modules[selected]) if selected is not None else set()
    if unknown_parameters:
        raise FrontendError("UNKNOWN_PARAMETER", f"Top {selected} has no overridable parameter(s): {', '.join(sorted(unknown_parameters))}")
    diagnostics = _diagnostics(compilation, manager)
    _check_sensitivity(compilation, manager)
    origins = []
    for name, module in sorted(modules.items()):
        location = _location(manager, module.header.name.location)
        end = _location(manager, module.sourceRange.end)
        origins.append({"module": name, **location, "end_line": end.get("line"), "end_column": end.get("column")})
    # SourceManager records files actually opened by the native preprocessor,
    # including headers that declare no module. Macro-only buffers have no path.
    source_files = set()
    for buffer in manager.getAllBuffers():
        path = Path(manager.getFullPath(buffer))
        if path.is_file():
            source_files.add(str(path.resolve()))
    return {"top": selected, "top_candidates": tops, "modules": sorted(modules), "origins": origins,
            "diagnostics": diagnostics, "source_files": sorted(source_files)}
