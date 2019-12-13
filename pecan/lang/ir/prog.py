#!/usr/bin/env python3.6
# -*- coding=utf-8 -*-

from colorama import Fore, Style

import time
import os
from functools import reduce

from lark import Lark, Transformer, v_args
import spot

from pecan.tools.automaton_tools import AutomatonTransformer, Substitution
from pecan.lang.ir.base import *
from pecan.settings import settings

class VarRef(IRExpression):
    def __init__(self, var_name):
        super().__init__()
        self.var_name = var_name
        self.is_int = False

    def evaluate(self, prog):
        # The automata accepts everything (because this isn't a predicate)
        return spot.formula('1').translate(), self

    def transform(self, transformer):
        return transformer.transform_VarRef(self)

    def show(self):
        return str(self.var_name)

    def __repr__(self):
        return self.show()

    def __eq__(self, other):
        return other is not None and type(other) is self.__class__ and self.var_name == other.var_name and self.get_type() == other.get_type()

    def __hash__(self):
        return hash((self.var_name, self.get_type()))

class AutLiteral(IRPredicate):
    def __init__(self, aut, display_node=None):
        super().__init__()
        self.aut = aut
        self.is_int = False
        self.display_node = display_node

    def evaluate(self, prog):
        return self.aut

    def transform(self, transformer):
        return transformer.transform_AutLiteral(self)

    def show(self):
        return repr(self)

    def __repr__(self):
        if self.display_node is not None:
            return 'AutLiteral({})'.format(repr(self.display_node))
        else:
            return 'AUTOMATON LITERAL'

    def __eq__(self, other):
        return other is not None and type(other) is self.__class__ and self.aut == other.aut

    def __hash__(self):
        return hash((self.aut))

class SpotFormula(IRPredicate):
    def __init__(self, formula_str):
        super().__init__()
        self.formula_str = formula_str

    def evaluate(self, prog):
        return spot.translate(self.formula_str)

    def transform(self, transformer):
        return transformer.transform_SpotFormula(self)

    def __repr__(self):
        return 'LTL({})'.format(self.formula_str)

    def __eq__(self, other):
        return other is not None and type(other) is self.__class__ and self.formula_str == other.formula_str

    def __hash__(self):
        return hash((self.formula_str))

class Match:
    def __init__(self, pred_name=None, pred_args=None, match_any=False):
        if pred_args is None:
            pred_args = []

        self.pred_name = pred_name
        self.pred_args = pred_args
        self.match_any = match_any

    def arity(self):
        return len(self.pred_args)

    def unify(self, other):
        if other.match_any:
            return self
        if self.match_any:
            return other

        new_args = []
        if self.pred_name != other.pred_name or self.arity() != other.arity():
            raise Exception(f'Could not unify {self} and {other}')

        for arg1, arg2 in zip(self.pred_args, other.pred_args):
            if arg1 == 'any' and arg2 == 'any':
                new_args.append('any')
            elif arg1 == 'any' and arg2 != 'any':
                new_args.append(arg2)
            elif arg1 != 'any' and arg2 == 'any':
                new_args.append(arg1)
            else:
                if arg1 == arg2:
                    new_args.append(arg1)
                else:
                    raise Exception(f'Could not unify {self} and {other}: cannot unify {arg1} and {arg2}')

        return Match(self.pred_name, new_args)

    def call_with(self, pred_name, unification, rest_args):
        if self.match_any:
            raise Exception(f'Predicate not found: {pred_name}')
        i = 0
        final_args = []
        for arg in self.pred_args:
            if arg.var_name == 'any':
                if i >= len(rest_args):
                    # TODO: We should check this in the linter probably
                    raise Exception(f'Not enough arguments to call {self}: {rest_args}')

                final_args.append(rest_args[i])
                i += 1
            else:
                final_args.append(VarRef(unification.get(arg.var_name, arg.var_name)))

        return Call(self.pred_name, final_args)

    def __repr__(self):
        return '{}({})'.format(self.pred_name, ', '.join(map(repr, self.pred_args)))

class Call(IRPredicate):
    def __init__(self, name, args):
        super().__init__()
        self.name = name
        self.args = [arg.with_parent(self) for arg in args]

    def arity(self):
        return len(self.args)

    def match(self):
        return Match(self.name, self.args)

    def with_args(self, new_args):
        return Call(self.name, new_args)

    def insert_first(self, new_arg):
        return Call(self.name, [new_arg] + self.args)

    def subs_first(self, new_arg):
        return self.with_args([new_arg] + self.args[1:])

    def evaluate_node(self, prog):
        # We may need to compute some values for the args
        arg_preds = []
        final_args = []
        for arg in self.args:
            # If it's not just a variable, we need to actually do something
            if type(arg) is not VarRef:
                new_var = VarRef(prog.fresh_name()).with_type(arg.get_type())
                # For some reason we need to import again here?
                from pecan.lang.ir.arith import Equals
                arg_preds.append((Equals(arg, new_var), new_var))
                final_args.append(new_var)
            else:
                final_args.append(arg)

        from pecan.lang.ir.bool import Conjunction
        from pecan.lang.ir.quant import Exists

        final_pred = AutLiteral(prog.call(self.name, final_args), display_node=Call(self.name, final_args))
        for pred, var in arg_preds:
            final_pred = Exists(var, None, Conjunction(pred, final_pred))

        return final_pred.evaluate(prog)

    def transform(self, transformer):
        return transformer.transform_Call(self)

    def __repr__(self):
        return '{}({})'.format(self.name, ', '.join(map(repr, self.args)))

class NamedPred(IRNode):
    def __init__(self, name, args, arg_restrictions, body, restriction_env=None, body_evaluated=None):
        super().__init__()
        self.name = name

        self.args = [arg.with_parent(self) for arg in args]
        self.arg_restrictions = {var.with_parent(self): restriction.with_parent(self) for var, restriction in arg_restrictions.items()}
        self.body = body.with_parent(self)

        self.restriction_env = restriction_env or {}

        self.body_evaluated = body_evaluated

    def evaluate(self, prog):
        # Here we keep track of all restrictions that were in scope when we are evaluated;
        # this essentially builds a closure. Otherwise, if we forget a variable after the declaration of this predicate,
        # then we will lose the restriction when we are called. This would cause our behavior to depend on lexically
        # where this predicate is used in the program, which would be confusing.
        prog.enter_scope()

        try:
            for _, arg_restriction in self.arg_restrictions.items():
                arg_restriction.evaluate(prog)

            self.restriction_env = prog.get_restriction_env()
        finally:
            prog.exit_scope()

    def transform(self, transformer):
        return transformer.transform_NamedPred(self)

    def arity(self):
        return len(self.args)

    def match(self):
        return Match(self.name, ['any'] * self.arity())

    def call(self, prog, arg_names=None):
        prog.enter_scope(dict(self.restriction_env))

        if self.body_evaluated is None:
            # We postprocess here because we will do it every time we call anyway (in AutomatonTransformer)
            self.body_evaluated = self.body.evaluate(prog).postprocess('BA')

        if arg_names is None or len(arg_names) == 0:
            result = self.body_evaluated
        else:
            subs_dict = {arg.var_name: spot.formula_ap(name.var_name) for arg, name in zip(self.args, arg_names)}
            substitution = Substitution(subs_dict)
            result = AutomatonTransformer(self.body_evaluated, substitution.substitute).transform()

        prog.exit_scope()

        return result

    def __repr__(self):
        if self.body_evaluated is None:
            return '{}({}) := {}'.format(self.name, ', '.join(map(repr, self.args)), self.body)
        else:
            return '{}({}) := {} (evaluated)'.format(self.name, ', '.join(map(repr, self.args)), self.body)

class Program(IRNode):
    def __init__(self, defs, *args, **kwargs):
        super().__init__()

        self.defs = [d.with_parent(self) for d in defs]
        self.preds = kwargs.get('preds', {})
        self.context = kwargs.get('context', {})
        self.restrictions = kwargs.get('restrictions', [{}])
        self.types = kwargs.get('types', {})
        self.eval_level = kwargs.get('eval_level', 0)
        self.result = kwargs.get('result', None)
        self.search_paths = kwargs.get('search_paths', [])

        from pecan.lang.type_inference import TypeInferer
        self.type_inferer = TypeInferer(self)

    def copy_defaults(self, other_prog):
        self.context = other_prog.context
        self.eval_level = other_prog.eval_level
        self.result = other_prog.result
        self.search_paths = other_prog.search_paths
        return self

    def include(self, other_prog):
        # Note: Intentionally do NOT merge restrictions, because it would be super confusing if variable restrictions "leaked" from imports
        self.preds.update({k: v.with_parent(self) for k, v in other_prog.preds.items()})
        self.context.update(other_prog.context)
        self.types.update({k: {pred_k: pred_v.with_parent(self) for pred_k, pred_v in v.items()} for k, v in other_prog.types.items()})

    def declare_type(self, pred_ref, val_dict):
        self.types[pred_ref] = val_dict

    def run_type_inference(self):
        from pecan.lang.ir.directives import DirectiveType, DirectiveForget, DirectiveLoadAut, DirectiveImport

        # TODO: Cleanup this part relative to evaluate below (e.g., lots of repeated if tree). Instead we could add a evaluate_type method or something, and let dispatch handle it for us
        for i, d in enumerate(self.defs):
            if type(d) is NamedPred:
                self.defs[i] = self.type_inferer.reset().transform(d).with_parent(self)
                self.preds[d.name] = self.defs[i]
                self.preds[d.name].evaluate(self)
                settings.log(0, self.preds[d.name])
            elif type(d) is Restriction:
                d.evaluate(self)
            elif type(d) is DirectiveForget:
                d.evaluate(self)
            elif type(d) is DirectiveType:
                d.evaluate(self)
            elif type(d) is DirectiveLoadAut:
                d.evaluate(self)
            elif type(d) is DirectiveImport:
                d.evaluate(self)

        # Clear all restrictions. All relevant restrictions will be held inside the restriction_env of the relevant predicates.
        # Having them also in our restrictions list just leads to double restricting, which is a waste of computation time
        self.restrictions.clear()

        return self

    def evaluate(self, old_env=None):
        from pecan.lang.ir.directives import DirectiveType, DirectiveForget, DirectiveLoadAut, DirectiveImport

        if old_env is not None:
            self.include(old_env)

        succeeded = True
        msgs = []

        for d in self.defs:
            settings.log(0, d)

            # Ignore these constructs because we should have run them earlier in run_type_inference
            if type(d) is NamedPred:
                # If we already computed it, it doesn't matter if we replace it with a more efficient version
                if self.preds[d.name].body_evaluated is None:
                    self.preds[d.name] = d
            elif type(d) is Restriction:
                pass
            elif type(d) is DirectiveType:
                pass
            elif type(d) is DirectiveForget:
                pass
            elif type(d) is DirectiveLoadAut:
                pass
            elif type(d) is DirectiveImport:
                pass

            else:
                result = d.evaluate(self)
                if result is not None and type(result) is Result:
                    if result.failed():
                        succeeded = False
                        msgs.append(result.message())

        self.result = Result('\n'.join(msgs), succeeded)

        return self

    def transform(self, transformer):
        return transformer.transform_Program(self)

    def forget(self, var_name):
        self.restrictions[-1].pop(var_name)

    def restrict(self, var_name, pred):
        if pred is not None and pred not in self.get_restrictions(var_name):
            if type(pred) is not Call or len(pred.args) == 0:
                raise Exception('Unexpected predicate used as restriction (must be Call with the first argument as the variable to restrict): {}'.format(pred))

            if var_name in self.restrictions[-1]:
                self.restrictions[-1][var_name].append(pred)
            else:
                self.restrictions[-1][var_name] = [pred]

    def get_restriction_env(self):
        result = {}

        for restriction_set in self.restrictions:
            result.update(restriction_set)

        return result

    def enter_scope(self, new_restrictions=None):
        if new_restrictions is None:
            new_restrictions = {}
        self.restrictions.append(dict(new_restrictions))

    def exit_scope(self):
        if len(self.restrictions) <= 0:
            raise Exception('Cannot exit the last scope!')
        else:
            self.restrictions.pop(-1)

    def get_restrictions(self, var_name: str):
        result = []
        for scope in self.restrictions:
            for r in scope.get(var_name, []):
                if not r in result:
                    result.append(r)
        return result

    def call(self, pred_name, args=None):
        try:
            if args is None or len(args) == 0:
                if pred_name in self.preds:
                    return self.preds[pred_name].call(self, args)
                else:
                    raise Exception(f'Predicate {pred_name}({args}) not found (known predicates: {self.preds.keys()}!')
            else:
                return self.dynamic_call(pred_name, args)
        except Exception as e:
            if pred_name in self.context:
                return self.call(self.context[pred_name], args)
            else:
                raise e

    def unify_with(self, a, b, unification):
        if b in unification:
            return unification[b] == a
        else:
            unification[b] = a
            return True

    def unify_type(self, t1, t2, unification):
        if type(t1) is VarRef and type(t2) is VarRef:
            return self.unify_with(t1.var_name, t2.var_name, unification)
        elif type(t1) is Call and type(t2) is Call:
            if t1.name != t2.name or len(t1.args) != len(t2.args):
                return False

            for arg1, arg2 in zip(t1.args, t2.args):
                if not self.unify_type(arg1, arg2, unification):
                    return False

            return True
        else:
            return False

    def try_unify_type(self, t1, t2, unification):
        old_unification = dict(unification)
        result = self.unify_type(t1, t2, unification)
        if result:
            return result
        else:
            # Do it this way so we mutate `unification` itself, and we don't want to change it unless we successfully unify
            unification.clear()
            unification.update(old_unification)
            return False

    def lookup_pred_by_name(self, pred_name):
        if pred_name in self.preds:
            return self.preds[pred_name]
        else:
            raise Exception(f'Predicate {pred_name} not found (known predicates: {self.preds.keys()}!')

    def lookup_call(self, pred_name, arg, unification):
        if arg.get_type() is None:
            return Match(match_any=True)

        for t in self.types:
            restriction = arg.get_type().restrict(arg)
            if self.try_unify_type(restriction, t.restrict(arg), unification):
                if pred_name in self.types[t]:
                    return self.types[t][pred_name].match()
                else:
                    return Match(match_any=True)

        return Match(match_any=True)

    def lookup_dynamic_call(self, pred_name, args):
        matches = []
        unification = {}
        for arg in args:
            match = self.lookup_call(pred_name, arg, unification)
            if match is None:
                raise Exception(f'No matching predicate found for {arg} called {pred_name}')
            matches.append(match)

        # There will always be at least one match because there should always be
        # at least one argument, so no need for an initial value
        final_match = reduce(lambda a, b: a.unify(b), matches, Match(match_any=True))

        # Match any means that we didn't find any type-specific matches
        if final_match.match_any:
            return Call(pred_name, args)
        else:
            return final_match.call_with(pred_name, unification, args)

    # Dynamic dispatch based on argument types
    def dynamic_call(self, pred_name, args):
        final_call = self.lookup_dynamic_call(pred_name, args)
        return self.lookup_pred_by_name(final_call.name).call(self, final_call.args)

    def locate_file(self, filename):
        for path in self.search_paths:
            try_path = os.path.join(path, filename)
            if os.path.exists(try_path):
                return try_path

        raise FileNotFoundError(filename)

    def __repr__(self):
        return repr(self.defs)

class Result:
    def __init__(self, msg, succeeded):
        self.msg = msg
        self.__succeeded = succeeded

    def succeeded(self):
        return self.__succeeded

    def failed(self):
        return not self.succeeded()

    def message(self):
        return self.msg

    def result_str(self):
        if self.succeeded():
            return f'{Fore.GREEN}{self.msg}{Style.RESET_ALL}'
        else:
            return f'{Fore.RED}{self.msg}{Style.RESET_ALL}'

class Restriction(IRNode):
    def __init__(self, restrict_vars, pred):
        super().__init__()
        self.restrict_vars = restrict_vars
        self.pred = pred.with_parent(self)

    def evaluate(self, prog):
        for var in self.restrict_vars:
            prog.restrict(var.var_name, self.pred.insert_first(var))

    def transform(self, transformer):
        return transformer.transform_Restriction(self)

    def __repr__(self):
        return '{} are {}'.format(', '.join(map(repr, self.restrict_vars)), self.pred)

