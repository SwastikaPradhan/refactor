import ast
import textwrap
import typing
from copy import deepcopy
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Union

import pytest

from refactor import BaseAction, Rule, Session, common, context
from refactor.actions import (
    EraseOrReplace,
    LazyInsertAfter,
    LazyReplace,
    Replace,
)
from refactor.context import Representative, Scope, ScopeType


class ReplaceNexts(Rule):
    INPUT_SOURCE = """
    def solution(Nexter: inputs):
        # blahblah some code here and there
        n = inputs.next_int()
        sub_process(inputs)
        st = inputs.next_str()
        sub_process(st)
    """

    EXPECTED_SOURCE = """
    def solution(Nexter: inputs):
        # blahblah some code here and there
        n = int(input())
        sub_process(inputs)
        st = str(input())
        sub_process(st)
    """

    def match(self, node):
        # We need a call
        assert isinstance(node, ast.Call)

        # on an attribute (inputs.xxx)
        assert isinstance(node.func, ast.Attribute)

        # where the name for attribute is `inputs`
        assert isinstance(node.func.value, ast.Name)
        assert node.func.value.id == "inputs"

        target_func_name = node.func.attr.removeprefix("next_")

        # make a call to target_func_name (e.g int) with input()
        target_func = ast.Call(
            ast.Name(target_func_name),
            args=[
                ast.Call(ast.Name("input"), args=[], keywords=[]),
            ],
            keywords=[],
        )
        return Replace(node, target_func)


class ReplacePlaceholders(Rule):
    INPUT_SOURCE = """
    def test():
        print(placeholder)
        print( # complicated
            placeholder
        )
        if placeholder is placeholder or placeholder > 32:
            print(3  + placeholder)
    """

    EXPECTED_SOURCE = """
    def test():
        print(42)
        print( # complicated
            42
        )
        if 42 is 42 or 42 > 32:
            print(3  + 42)
    """

    def match(self, node):
        assert isinstance(node, ast.Name)
        assert node.id == "placeholder"

        replacement = ast.Constant(42)
        return Replace(node, replacement)


class PropagateConstants(Rule):
    INPUT_SOURCE = """
    a = 1

    def main(d = 5):
        b = 4
        c = a + b
        e = 3
        e = 4
        return c + (b * 3) + d + e

    class T:
        b = 2
        print(a + b + c)

        def foo():
            c = 3
            print(a + b + c + d)
    """

    EXPECTED_SOURCE = """
    a = 1

    def main(d = 5):
        b = 4
        c = a + 4
        e = 3
        e = 4
        return c + (4 * 3) + d + e

    class T:
        b = 2
        print(a + 2 + c)

        def foo():
            c = 3
            print(a + b + 3 + d)
    """

    context_providers = (Scope,)

    def match(self, node):
        assert isinstance(node, ast.Name)
        assert isinstance(node.ctx, ast.Load)

        current_scope = self.context["scope"].resolve(node)
        assert current_scope.defines(node.id)

        definitions = current_scope.definitions[node.id]

        assert len(definitions) == 1
        assert isinstance(definition := definitions[0], ast.Assign)
        assert isinstance(value := definition.value, ast.Constant)

        return Replace(node, value)


class ImportFinder(Representative):
    def collect(self, name, scope):
        import_statents = [
            node
            for node in ast.walk(self.context.tree)
            if isinstance(node, ast.ImportFrom)
            if node.module == name
            if scope.can_reach(self.context["scope"].resolve(node))
        ]

        names = {}
        for import_statement in import_statents:
            for alias in import_statement.names:
                names[alias.name] = import_statement

        return names


@dataclass
class AddNewImport(LazyInsertAfter):
    module: str
    names: List[str]

    def build(self):
        return ast.ImportFrom(
            level=0,
            module=self.module,
            names=[ast.alias(name) for name in self.names],
        )


@dataclass
class ModifyExistingImport(LazyReplace):
    name: str

    def build(self):
        new_node = self.branch()
        new_node.names.append(ast.alias(self.name))
        return new_node


class TypingAutoImporter(Rule):
    INPUT_SOURCE = """
    import lol
    from something import another

    def foo(items: List[Optional[str]]) -> Dict[str, List[Tuple[int, ...]]]:
        class Something:
            no: Iterable[int]

            def bar(self, context: Dict[str, int]) -> List[int]:
                print(1)
    """

    EXPECTED_SOURCE = """
    import lol
    from something import another
    from typing import Dict, List, Iterable, Optional, Tuple

    def foo(items: List[Optional[str]]) -> Dict[str, List[Tuple[int, ...]]]:
        class Something:
            no: Iterable[int]

            def bar(self, context: Dict[str, int]) -> List[int]:
                print(1)
    """

    context_providers = (ImportFinder, context.Scope)

    def find_last_import(self, tree):
        assert isinstance(tree, ast.Module)
        for index, node in enumerate(tree.body, -1):
            if isinstance(node, ast.Expr) and isinstance(
                node.value, ast.Constant
            ):
                continue
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            else:
                break

        return tree.body[index]

    def match(self, node):
        assert isinstance(node, ast.Name)
        assert isinstance(node.ctx, ast.Load)
        assert node.id in typing.__all__
        assert not node.id.startswith("__")

        scope = self.context["scope"].resolve(node)
        typing_imports = self.context["import_finder"].collect(
            "typing", scope=scope
        )

        if len(typing_imports) == 0:
            last_import = self.find_last_import(self.context.tree)
            return AddNewImport(last_import, "typing", [node.id])

        assert len(typing_imports) >= 1
        assert node.id not in typing_imports

        closest_import = common.find_closest(node, *typing_imports.values())
        return ModifyExistingImport(closest_import, node.id)


class AsyncifierAction(LazyReplace):
    def build(self):
        new_node = self.branch()
        new_node.__class__ = ast.AsyncFunctionDef
        return new_node


class MakeFunctionAsync(Rule):
    INPUT_SOURCE = """
    def something():
        a += .1
        '''you know
            this is custom
                literal
        '''
        print(we,
            preserve,
                everything
        )
        return (
            right + "?")
    """

    EXPECTED_SOURCE = """
    async def something():
        a += .1
        '''you know
            this is custom
                literal
        '''
        print(we,
            preserve,
                everything
        )
        return (
            right + "?")
    """

    def match(self, node):
        assert isinstance(node, ast.FunctionDef)
        return AsyncifierAction(node)


class OnlyKeywordArgumentDefaultNotSetCheckRule(Rule):
    context_providers = (context.Scope,)

    INPUT_SOURCE = """
        class Klass:
            def method(self, *, a):
                print()

            lambda self, *, a: print

        """

    EXPECTED_SOURCE = """
        class Klass:
            def method(self, *, a=None):
                print()

            lambda self, *, a=None: print

        """

    def match(self, node: ast.AST) -> Optional[BaseAction]:
        assert isinstance(node, (ast.FunctionDef, ast.Lambda))
        assert any(kw_default is None for kw_default in node.args.kw_defaults)

        if isinstance(node, ast.Lambda) and not (
            isinstance(node.body, ast.Name)
            and isinstance(node.body.ctx, ast.Load)
        ):
            scope = self.context["scope"].resolve(node.body)
            scope.definitions.get(node.body.id, [])

        elif isinstance(node, ast.FunctionDef):
            for stmt in node.body:
                for identifier in ast.walk(stmt):
                    if not (
                        isinstance(identifier, ast.Name)
                        and isinstance(identifier.ctx, ast.Load)
                    ):
                        continue

                    scope = self.context["scope"].resolve(identifier)
                    while not scope.definitions.get(identifier.id, []):
                        scope = scope.parent
                        if scope is None:
                            break

        kw_defaults = []
        for kw_default in node.args.kw_defaults:
            if kw_default is None:
                kw_defaults.append(ast.Constant(value=None))
            else:
                kw_defaults.append(kw_default)

        target = deepcopy(node)
        target.args.kw_defaults = kw_defaults

        return Replace(node, target)


class InternalizeFunctions(Rule):
    INPUT_SOURCE = """
        __all__ = ["regular"]
        def regular():
            pass

        def foo():


            return easy_to_fool_me

        def               bar (                    ):
                return maybe_indented

        def        \
            maybe \
                (more):
                    return complicated

        if indented_1:
            if indented_2:
                def normal():
                    return normal

        @dataclass
        class Zebra:
            def does_not_matter():
                pass

        @deco
        async \
            def \
                async_function():
                    pass
        """

    EXPECTED_SOURCE = """
        __all__ = ["regular"]
        def regular():
            pass

        def _foo():


            return easy_to_fool_me

        def               _bar (                    ):
                return maybe_indented

        def        \
            _maybe \
                (more):
                    return complicated

        if indented_1:
            if indented_2:
                def _normal():
                    return normal

        @dataclass
        class _Zebra:
            def does_not_matter():
                pass

        @deco
        async \
            def \
                _async_function():
                    pass
        """

    def _get_public_functions(self) -> Optional[Sequence[str]]:
        # __all__ generally contains only a list/tuple of strings
        # so it should be easy to infer.

        global_scope = self.context.scope.global_scope

        try:
            [raw_definition] = global_scope.get_definitions("__all__") or []
        except ValueError:
            return None

        assert isinstance(raw_definition, ast.Assign)

        try:
            return ast.literal_eval(raw_definition.value)
        except ValueError:
            return None

    def match(self, node: ast.AST) -> Replace:
        assert isinstance(
            node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)
        )
        assert not node.name.startswith("_")

        node_scope = self.context.scope.resolve(node)
        assert node_scope.scope_type is ScopeType.GLOBAL

        public_functions = self._get_public_functions()
        assert public_functions is not None
        assert node.name not in public_functions

        new_node = common.clone(node)
        new_node.name = "_" + node.name
        return Replace(node, new_node)


class RemoveDeadCode(Rule):
    INPUT_SOURCE = """
    CONSTANT_1 = True
    CONSTANT_2 = False
    CONSTANT_3 = 1
    CONSTANT_4 = 0
    CONSTANT_5 = uninferrable()
    if CONSTANT_1:
        pass
    if CONSTANT_2:
        pass
    if CONSTANT_3:
        pass
    if CONSTANT_4:
        pass
    if CONSTANT_5:
        pass
    def f():
        if CONSTANT_1:
            pass
    def f():
        if CONSTANT_1:
            if CONSTANT_2:
                if CONSTANT_3:
                    pass
    def f():
        if CONSTANT_1:
            pass
        if CONSTANT_2:
            pass
    def f3():
        if CONSTANT_2:
            pass
        return
    def f4():
        try:
            if CONSTANT_2:
                pass
        except Exception:
            z = 4
            if CONSTANT_2:
                pass
        finally:
            if CONSTANT_4:
                pass
    for function in f():
        if CONSTANT_5:
            pass
    else:
        if CONSTANT_2:
            pass
    for function in f():
        if CONSTANT_2:
            pass
        a = 1
    else:
        b = 2
        if CONSTANT_2:
            pass
    """

    EXPECTED_SOURCE = """
        CONSTANT_1 = True
        CONSTANT_2 = False
        CONSTANT_3 = 1
        CONSTANT_4 = 0
        CONSTANT_5 = uninferrable()
        if CONSTANT_1:
            pass
        if CONSTANT_3:
            pass
        if CONSTANT_5:
            pass
        def f():
            if CONSTANT_1:
                pass
        def f():
            if CONSTANT_1:
                pass
        def f():
            if CONSTANT_1:
                pass
        def f3():
            return
        def f4():
            try:
                pass
            except Exception:
                z = 4
            finally:
                pass
        for function in f():
            if CONSTANT_5:
                pass
        else:
            pass
        for function in f():
            a = 1
        else:
            b = 2
    """

    def match(self, node: ast.AST) -> Optional[EraseOrReplace]:
        assert isinstance(node, ast.If)

        if isinstance(node.test, ast.Constant):
            static_condition = node.test.value
        elif isinstance(node.test, ast.Name):
            node_scope = self.context.scope.resolve(node)
            definitions = node_scope.get_definitions(node.test.id) or []
            assert len(definitions) == 1 and isinstance(
                definition := definitions[0], ast.Assign
            )
            assert isinstance(definition.value, ast.Constant)
            static_condition = definition.value
        else:
            return None

        assert not static_condition.value
        assert not node.orelse
        return EraseOrReplace(node)


class DownstreamAnalyzer(Representative):
    context_providers = (context.Scope,)

    def iter_dependents(
        self, name: str, source: Union[ast.Import, ast.ImportFrom]
    ) -> Iterator[ast.Name]:
        for node in ast.walk(self.context.tree):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id == name
            ):
                node_scope = self.context.scope.resolve(node)
                definitions = node_scope.get_definitions(name) or []
                if any(definition is source for definition in definitions):
                    yield node


class RenameImportAndDownstream(Rule):
    context_providers = (DownstreamAnalyzer,)

    INPUT_SOURCE = """
        import a

        a.do_something()

        for _ in a.iter():
            print(
                a
                        + 1
                           + 3
            )

        @a.series
        def f():
            import a

            class Z(a.Backport):
                meth = a.method

            return a.backport()

        a

        def multi():
            if A:
                if B:
                    import a
                else:
                    import a
            else:
                import a

            for _ in range(x):
                a.do_something()

            return a.dot()
        """

    EXPECTED_SOURCE = """
        import b

        b.do_something()

        for _ in b.iter():
            print(
                b
                        + 1
                           + 3
            )

        @b.series
        def f():
            import b

            class Z(b.Backport):
                meth = b.method

            return b.backport()

        b

        def multi():
            if A:
                if B:
                    import b
                else:
                    import b
            else:
                import b

            for _ in range(x):
                b.do_something()

            return b.dot()
        """

    def match(self, node: ast.AST) -> Iterator[Replace]:
        assert isinstance(node, (ast.Import, ast.ImportFrom))

        aliases = [alias for alias in node.names if alias.name == "a"]
        assert len(aliases) == 1

        [alias] = aliases
        for dependent in self.context.downstream_analyzer.iter_dependents(
            alias.asname or alias.name, node
        ):
            yield Replace(dependent, ast.Name("b", ast.Load()))

        replacement = common.clone(node)
        replacement.names[node.names.index(alias)].name = "b"
        yield Replace(node, replacement)


class AssertEncoder(Rule):
    INPUT_SOURCE = """
        print(hello)
        assert "aaaaaBBBBcccc", "len=1"
        print('''
        test🥰🥰🥰
        © ®© ®
        ''')
        assert "©© ®®copyrighted®® ©©©", "len=2"
        print(hello)
        if something:
            assert (
                "🥰 😎 😇 print\
                    🥰 😎 😇"
            ), "some emojisss"

            def ensure():
                assert "€urre€y of eu™", "len=3"

        print("refactor  🚀 🚀")
    """

    EXPECTED_SOURCE = """
        print(hello)
        assert decrypt('<aaaaaBBBBcccc>'), "len=1"
        print('''
        test🥰🥰🥰
        © ®© ®
        ''')
        assert decrypt('<©© ®®copyrighted®® ©©©>'), "len=2"
        print(hello)
        if something:
            assert (
                decrypt('<🥰 😎 😇 print                    🥰 😎 😇>')
            ), "some emojisss"

            def ensure():
                assert decrypt('<€urre€y of eu™>'), "len=3"

        print("refactor  🚀 🚀")
    """

    def match(self, node: ast.AST) -> Replace:
        assert isinstance(node, ast.Assert)
        assert isinstance(test := node.test, ast.Constant)
        assert isinstance(inner_text := test.value, str)

        encrypt_call = ast.Call(
            func=ast.Name("decrypt"),
            args=[ast.Constant(f"<{inner_text}>")],
            keywords=[],
        )
        return Replace(test, encrypt_call)


@pytest.mark.parametrize(
    "rule",
    [
        ReplaceNexts,
        ReplacePlaceholders,
        PropagateConstants,
        TypingAutoImporter,
        MakeFunctionAsync,
        OnlyKeywordArgumentDefaultNotSetCheckRule,
        InternalizeFunctions,
        RemoveDeadCode,
        RenameImportAndDownstream,
        AssertEncoder,
    ],
)
def test_complete_rules(rule):
    session = Session([rule])

    source_code = textwrap.dedent(rule.INPUT_SOURCE)
    try:
        ast.parse(source_code)
    except SyntaxError:
        pytest.fail("Input source is not valid Python code")

    assert session.run(source_code) == textwrap.dedent(rule.EXPECTED_SOURCE)
