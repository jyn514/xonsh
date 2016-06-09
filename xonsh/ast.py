# -*- coding: utf-8 -*-
"""The xonsh abstract syntax tree node."""
# These are imported into our module namespace for the benefit of parser.py.
# pylint: disable=unused-import
from ast import Module, Num, Expr, Str, Bytes, UnaryOp, UAdd, USub, Invert, \
    BinOp, Add, Sub, Mult, Div, FloorDiv, Mod, Pow, Compare, Lt, Gt, \
    LtE, GtE, Eq, NotEq, In, NotIn, Is, IsNot, Not, BoolOp, Or, And, \
    Subscript, Load, Slice, ExtSlice, List, Tuple, Set, Dict, AST, NameConstant, \
    Name, GeneratorExp, Store, comprehension, ListComp, SetComp, DictComp, \
    Assign, AugAssign, BitXor, BitAnd, BitOr, LShift, RShift, Assert, Delete, \
    Del, Pass, Raise, Import, alias, ImportFrom, Continue, Break, Yield, \
    YieldFrom, Return, IfExp, Lambda, arguments, arg, Call, keyword, \
    Attribute, Global, Nonlocal, If, While, For, withitem, With, Try, \
    ExceptHandler, FunctionDef, ClassDef, Starred, NodeTransformer, \
    Interactive, Expression, Index, literal_eval, dump, walk
from ast import Ellipsis  # pylint: disable=redefined-builtin
# pylint: enable=unused-import
import textwrap
from itertools import repeat

from xonsh.tools import subproc_toks, find_next_break
from xonsh.platform import PYTHON_VERSION_INFO

if PYTHON_VERSION_INFO >= (3, 5, 0):
    # pylint: disable=unused-import
    # pylint: disable=no-name-in-module
    from ast import MatMult, AsyncFunctionDef, AsyncWith, AsyncFor, Await
else:
    MatMult = AsyncFunctionDef = AsyncWith = AsyncFor = Await = None

STATEMENTS = (FunctionDef, ClassDef, Return, Delete, Assign, AugAssign, For,
              While, If, With, Raise, Try, Assert, Import, ImportFrom, Global,
              Nonlocal, Expr, Pass, Break, Continue)


def leftmostname(node):
    """Attempts to find the first name in the tree."""
    if isinstance(node, Name):
        rtn = node.id
    elif isinstance(node, (BinOp, Compare)):
        rtn = leftmostname(node.left)
    elif isinstance(node, (Attribute, Subscript, Starred, Expr)):
        rtn = leftmostname(node.value)
    elif isinstance(node, Call):
        rtn = leftmostname(node.func)
    elif isinstance(node, UnaryOp):
        rtn = leftmostname(node.operand)
    elif isinstance(node, BoolOp):
        rtn = leftmostname(node.values[0])
    elif isinstance(node, Assign):
        rtn = leftmostname(node.targets[0])
    elif isinstance(node, (Str, Bytes)):
        # handles case of "./my executable"
        rtn = leftmostname(node.s)
    elif isinstance(node, Tuple) and len(node.elts) > 0:
        # handles case of echo ,1,2,3
        rtn = leftmostname(node.elts[0])
    else:
        rtn = None
    return rtn


def get_lineno(node, default=0):
    """Gets the lineno of a node or returns the default."""
    return getattr(node, 'lineno', default)


def min_line(node):
    """Computes the minimum lineno."""
    node_line = get_lineno(node)
    return min(map(get_lineno, walk(node), repeat(node_line)))


def max_line(node):
    """Computes the maximum lineno."""
    return max(map(get_lineno, walk(node)))


def get_col(node, default=-1):
    """Gets the col_offset of a node, or returns the default"""
    return getattr(node, 'col_offset', default)


def min_col(node):
    """Computes the minimum col_offset."""
    return min(map(get_col, walk(node), repeat(node.col_offset)))


def max_col(node):
    """Returns the maximum col_offset of the node and all sub-nodes."""
    col = getattr(node, 'max_col', None)
    if col is None:
        col = max(map(get_col, walk(node)))
    return col


def get_id(node, default=None):
    """Gets the id attribute of a node, or returns a default."""
    return getattr(node, 'id', default)


def gather_names(node):
    """Returns the set of all names present in the node's tree."""
    rtn = set(map(get_id, walk(node)))
    rtn.discard(None)
    return rtn


def has_elts(x):
    """Tests if x is an AST node with elements."""
    return isinstance(x, AST) and hasattr(x, 'elts')


def xonsh_call(name, args, lineno=None, col=None):
    """Creates the AST node for calling a function of a given name."""
    return Call(func=Name(id=name, ctx=Load(), lineno=lineno, col_offset=col),
                args=args, keywords=[], starargs=None, kwargs=None,
                lineno=lineno, col_offset=col)


def isdescendable(node):
    """Deteremines whether or not a node is worth visiting. Currently only
    UnaryOp and BoolOp nodes are visited.
    """
    return isinstance(node, (UnaryOp, BoolOp))


class CtxAwareTransformer(NodeTransformer):
    """Transforms a xonsh AST based to use subprocess calls when
    the first name in an expression statement is not known in the context.
    This assumes that the expression statement is instead parseable as
    a subprocess.
    """

    def __init__(self, parser):
        """Parameters
        ----------
        parser : xonsh.Parser
            A parse instance to try to parse suprocess statements with.
        """
        super(CtxAwareTransformer, self).__init__()
        self.parser = parser
        self.input = None
        self.contexts = []
        self.lines = None
        self.mode = None
        self._nwith = 0

    def ctxvisit(self, node, inp, ctx, mode='exec'):
        """Transforms the node in a context-dependent way.

        Parameters
        ----------
        node : ast.AST
            A syntax tree to transform.
        input : str
            The input code in string format.
        ctx : dict
            The root context to use.

        Returns
        -------
        node : ast.AST
            The transformed node.
        """
        self.lines = inp.splitlines()
        self.contexts = [ctx, set()]
        self.mode = mode
        self._nwith = 0
        node = self.visit(node)
        del self.lines, self.contexts, self.mode
        self._nwith = 0
        return node

    def ctxupdate(self, iterable):
        """Updated the most recent context."""
        self.contexts[-1].update(iterable)

    def ctxadd(self, value):
        """Adds a value the most recent context."""
        self.contexts[-1].add(value)

    def ctxremove(self, value):
        """Removes a value the most recent context."""
        for ctx in reversed(self.contexts):
            if value in ctx:
                ctx.remove(value)
                break

    def try_subproc_toks(self, node, strip_expr=False):
        """Tries to parse the line of the node as a subprocess."""
        line = self.lines[node.lineno - 1]
        if self.mode == 'eval':
            mincol = len(line) - len(line.lstrip())
            maxcol = None
        else:
            mincol = min_col(node)
            maxcol = max_col(node)
            if mincol == maxcol:
                maxcol = find_next_break(line, mincol=mincol,
                                         lexer=self.parser.lexer)
            else:
                maxcol += 1
        spline = subproc_toks(line,
                              mincol=mincol,
                              maxcol=maxcol,
                              returnline=False,
                              lexer=self.parser.lexer)
        if spline is None:
            return node
        try:
            newnode = self.parser.parse(spline, mode=self.mode)
            newnode = newnode.body
            if not isinstance(newnode, AST):
                # take the first (and only) Expr
                newnode = newnode[0]
            newnode.lineno = node.lineno
            newnode.col_offset = node.col_offset
        except SyntaxError:
            newnode = node
        if strip_expr and isinstance(newnode, Expr):
            newnode = newnode.value
        return newnode

    def is_in_scope(self, node):
        """Determines whether or not the current node is in scope."""
        lname = leftmostname(node)
        if lname is None:
            return node
        inscope = False
        for ctx in reversed(self.contexts):
            if lname in ctx:
                inscope = True
                break
        return inscope

    #
    # With Transformers
    #
    def insert_with_block_check(self, node):
        """Modifies a with statement node in-place to add an initial check
        for whether or not the block should be executed. If the block is
        not executed it will raise a XonshBlockError containing the required
        information.
        """
        nwith = self._nwith  # the nesting level of the current with-statement
        lineno = get_lineno(node)
        col = get_col(node, 0)
        # Add or discover target names
        targets = set()
        i = 0  # index of unassigned items

        def make_next_target():
            nonlocal i
            targ = '__xonsh_with_target_{}_{}__'.format(nwith, i)
            n = Name(id=targ, ctx=Store(), lineno=lineno, col_offset=col)
            targets.add(targ)
            i += 1
            return n
        for item in node.items:
            if item.optional_vars is None:
                if has_elts(item.context_expr):
                    targs = [make_next_target() for _ in item.context_expr.elts]
                    optvars = Tuple(elts=targs, ctx=Store(), lineno=lineno,
                                    col_offset=col)
                else:
                    optvars = make_next_target()
                item.optional_vars = optvars
            else:
                targets.update(gather_names(item.optional_vars))
        # Ok, now that targets have been found / created, make the actual check
        # to see if we are in a non-executing block. This is equivalent to
        # writing the following condition:
        #
        #     if getattr(targ0, '__xonsh_block__', False) or \
        #        getattr(targ1, '__xonsh_block__', False) or ...:
        #         raise XonshBlockError(lines, globals(), locals())
        tests = [_getblockattr(t, lineno, col) for t in sorted(targets)]
        if len(tests) == 1:
            test = tests[0]
        else:
            test = BoolOp(op=Or(), values=tests, lineno=lineno, col_offset=col)
        ldx, udx = self._find_with_block_line_idx(node)
        lines = [Str(s=s, lineno=lineno, col_offset=col)
                 for s in self.lines[ldx:udx]]
        check = If(test=test, body=[
            Raise(exc=xonsh_call('XonshBlockError',
                                 args=[List(elts=lines, ctx=Load(),
                                            lineno=lineno, col_offset=col),
                                       xonsh_call('globals', args=[],
                                                  lineno=lineno, col=col),
                                       xonsh_call('locals', args=[],
                                                  lineno=lineno, col=col)],
                                 lineno=lineno, col=col),
                  cause=None, lineno=lineno, col_offset=col)],
                orelse=[], lineno=lineno, col_offset=col)
        node.body.insert(0, check)

    def _find_with_block_line_idx(self, node):
        ldx = min_line(node.body[0]) - 1
        udx = max_line(node.body[-1])
        # now check if parsable, or add lines until it is or we run out of lines
        nlines = len(self.lines)
        lines = 'with __xonsh_dummy__:\n' + '\n'.join(self.lines[ldx:udx])
        lines += '\n'
        parsable = False
        while not parsable and udx < nlines:
            try:
                self.parser.parse(lines, mode=self.mode)
                parsable = True
            except SyntaxError:
                lines += self.lines[udx] + '\n'
                udx += 1
        return ldx, udx

    #
    # Replacement visitors
    #

    def visit_Expression(self, node):
        """Handle visiting an expression body."""
        if isdescendable(node.body):
            node.body = self.visit(node.body)
        body = node.body
        inscope = self.is_in_scope(body)
        if not inscope:
            node.body = self.try_subproc_toks(body)
        return node

    def visit_Expr(self, node):
        """Handle visiting an expression."""
        if isdescendable(node.value):
            node.value = self.visit(node.value)  # this allows diving into BoolOps
        if self.is_in_scope(node):
            return node
        else:
            newnode = self.try_subproc_toks(node)
            if not isinstance(newnode, Expr):
                newnode = Expr(value=newnode,
                               lineno=node.lineno,
                               col_offset=node.col_offset)
                if hasattr(node, 'max_lineno'):
                    newnode.max_lineno = node.max_lineno
                    newnode.max_col = node.max_col
            return newnode

    def visit_UnaryOp(self, node):
        """Handle visiting an unary operands, like not."""
        if isdescendable(node.operand):
            node.operand = self.visit(node.operand)
        operand = node.operand
        inscope = self.is_in_scope(operand)
        if not inscope:
            node.operand = self.try_subproc_toks(operand, strip_expr=True)
        return node

    def visit_BoolOp(self, node):
        """Handle visiting an boolean operands, like and/or."""
        for i in range(len(node.values)):
            val = node.values[i]
            if isdescendable(val):
                val = node.values[i] = self.visit(val)
            inscope = self.is_in_scope(val)
            if not inscope:
                node.values[i] = self.try_subproc_toks(val, strip_expr=True)
        return node

    #
    # Context aggregator visitors
    #

    def visit_Assign(self, node):
        """Handle visiting an assignment statement."""
        ups = set()
        for targ in node.targets:
            if isinstance(targ, (Tuple, List)):
                ups.update(leftmostname(elt) for elt in targ.elts)
            elif isinstance(targ, BinOp):
                newnode = self.try_subproc_toks(node)
                if newnode is node:
                    ups.add(leftmostname(targ))
                else:
                    return newnode
            else:
                ups.add(leftmostname(targ))
        self.ctxupdate(ups)
        return node

    def visit_Import(self, node):
        """Handle visiting a import statement."""
        for name in node.names:
            if name.asname is None:
                self.ctxadd(name.name)
            else:
                self.ctxadd(name.asname)
        return node

    def visit_ImportFrom(self, node):
        """Handle visiting a "from ... import ..." statement."""
        for name in node.names:
            if name.asname is None:
                self.ctxadd(name.name)
            else:
                self.ctxadd(name.asname)
        return node

    def visit_With(self, node):
        """Handle visiting a with statement."""
        for item in node.items:
            if item.optional_vars is not None:
                self.ctxupdate(gather_names(item.optional_vars))
        self._nwith += 1
        self.generic_visit(node)
        self._nwith -= 1
        self.insert_with_block_check(node)
        return node

    def visit_For(self, node):
        """Handle visiting a for statement."""
        targ = node.target
        if isinstance(targ, (Tuple, List)):
            self.ctxupdate(leftmostname(elt) for elt in targ.elts)
        else:
            self.ctxadd(leftmostname(targ))
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node):
        """Handle visiting a function definition."""
        self.ctxadd(node.name)
        self.contexts.append(set())
        self.generic_visit(node)
        self.contexts.pop()
        return node

    def visit_ClassDef(self, node):
        """Handle visiting a class definition."""
        self.ctxadd(node.name)
        self.contexts.append(set())
        self.generic_visit(node)
        self.contexts.pop()
        return node

    def visit_Delete(self, node):
        """Handle visiting a del statement."""
        for targ in node.targets:
            if isinstance(targ, Name):
                self.ctxremove(targ.id)
        self.generic_visit(node)
        return node

    def visit_Try(self, node):
        """Handle visiting a try statement."""
        for handler in node.handlers:
            if handler.name is not None:
                self.ctxadd(handler.name)
        self.generic_visit(node)
        return node

    def visit_Global(self, node):
        """Handle visiting a global statement."""
        self.contexts[1].update(node.names)  # contexts[1] is the global ctx
        self.generic_visit(node)
        return node



def pdump(s, **kwargs):
    """performs a pretty dump of an AST node."""
    if isinstance(s, AST):
        s = dump(s, **kwargs).replace(',', ',\n')
    openers = '([{'
    closers = ')]}'
    lens = len(s) + 1
    if lens == 1:
        return s
    i = min([s.find(o)%lens for o in openers])
    if i == lens - 1:
        return s
    closer = closers[openers.find(s[i])]
    j = s.rfind(closer)
    if j == -1 or j <= i:
        return s[:i+1] + '\n' + textwrap.indent(pdump(s[i+1:]), ' ')
    pre = s[:i+1] + '\n'
    mid = s[i+1:j]
    post = '\n' + s[j:]
    mid = textwrap.indent(pdump(mid), ' ')
    if '(' in post or '[' in post or '{' in post:
        post = pdump(post)
    return pre + mid + post


def pprint(s, *, sep=None, end=None, file=None, flush=False, **kwargs):
    """Performs a pretty print of the AST nodes."""
    print(pdump(s, **kwargs), sep=sep, end=end, file=file, flush=flush)


#
# Private helpers
#

def _getblockattr(name, lineno, col):
    """calls getattr(name, '__xonsh_block__', False)."""
    return xonsh_call('getattr', args=[
        Name(id=name, ctx=Load(), lineno=lineno, col_offset=col),
        Str(s='__xonsh_block__', lineno=lineno, col_offset=col),
        NameConstant(value=False, lineno=lineno, col_offset=col)],
        lineno=lineno, col=col)
