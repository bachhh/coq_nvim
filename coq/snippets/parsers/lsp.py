from pathlib import PurePath
from string import ascii_letters, ascii_lowercase, digits
from typing import AbstractSet, MutableSequence, Optional

from ...shared.types import Context
from .parser import context_from, next_char, pushback_chars, raise_err, token_parser
from .types import (
    Begin,
    DummyBegin,
    End,
    Parsed,
    ParseInfo,
    ParserCtx,
    TokenStream,
    Unparsed,
)

#
# O(n) single pass LSP Parser:
# https://github.com/microsoft/language-server-protocol/blob/master/snippetSyntax.md
#


"""
any         ::= tabstop | placeholder | choice | variable | text
tabstop     ::= '$' int | '${' int '}'
placeholder ::= '${' int ':' any '}'
choice      ::= '${' int '|' text (',' text)* '|}'
variable    ::= '$' var | '${' var }'
                | '${' var ':' any '}'
                | '${' var '/' regex '/' (format | text)+ '/' options '}'
format      ::= '$' int | '${' int '}'
                | '${' int ':' '/upcase' | '/downcase' | '/capitalize' '}'
                | '${' int ':+' if '}'
                | '${' int ':?' if ':' else '}'
                | '${' int ':-' else '}' | '${' int ':' else '}'
regex       ::= JavaScript Regular Expression value (ctor-string)
options     ::= JavaScript Regular Expression option (ctor-options)
var         ::= [_a-zA-Z] [_a-zA-Z0-9]*
int         ::= [0-9]+
text        ::= .*
"""


_ESC_CHARS = {"\\", "$", "}"}
_REGEX_ESC_CHARS = {"\\", "/"}
_CHOICE_ESC_CHARS = _ESC_CHARS | {",", "|"}
_INT_CHARS = {*digits}
_VAR_BEGIN_CHARS = {*ascii_letters}
_VAR_CHARS = {*digits, *ascii_letters, "_"}
_REGEX_FLAG_CHARS = {*ascii_lowercase}


def _parse_escape(context: ParserCtx, *, escapable_chars: AbstractSet[str]) -> str:
    pos, char = next_char(context)
    assert char == "\\"

    pos, char = next_char(context)
    if char in escapable_chars:
        return char
    else:
        raise_err(
            text=context.text,
            pos=pos,
            condition="after \\",
            expected=escapable_chars,
            actual=char,
        )


# choice      ::= '${' int '|' text (',' text)* '|}'
def _half_parse_choice(context: ParserCtx) -> TokenStream:
    pos, char = next_char(context)
    assert char == "|"

    yield " "
    for pos, char in context:
        if char == "\\":
            pushback_chars(context, (pos, char))
            yield _parse_escape(context, escapable_chars=_CHOICE_ESC_CHARS)
        elif char == "|":
            pos, char = next_char(context)
            if char == "}":
                yield " "
                yield End()
                break
            else:
                raise_err(
                    text=context.text,
                    pos=pos,
                    condition="after |",
                    expected=("}",),
                    actual=char,
                )
        elif char == ",":
            yield "|"
        else:
            yield char


# tabstop | choice | placeholder
# -- all starts with (int)
def _parse_tcp(context: ParserCtx) -> TokenStream:
    idx_acc: MutableSequence[str] = []

    for pos, char in context:
        if char in _INT_CHARS:
            idx_acc.append(char)
        else:
            yield Begin(idx=int("".join(idx_acc)))
            if char == "}":
                # tabstop     ::= '$' int | '${' int '}'
                yield End()
                break
            elif char == "|":
                # choice      ::= '${' int '|' text (',' text)* '|}'
                pushback_chars(context, (pos, char))
                yield from _half_parse_choice(context)
                break
            elif char == ":":
                # placeholder ::= '${' int ':' any '}'
                context.state.depth += 1
                break
            else:
                raise_err(
                    text=context.text,
                    pos=pos,
                    condition="while parsing (tabstop | choice | placeholder)",
                    expected=("0-9", "|", ":"),
                    actual=char,
                )


def _variable_substitution(context: ParserCtx, *, var_name: str) -> Optional[str]:
    ctx = context.ctx
    row, _ = ctx.position
    path = PurePath(ctx.filename)

    if var_name == "TM_SELECTED_TEXT":
        return context.info.visual

    elif var_name == "TM_CURRENT_LINE":
        return ctx.line

    elif var_name == "TM_CURRENT_WORD":
        return ctx.words

    elif var_name == "TM_LINE_INDEX":
        return str(row)

    elif var_name == "TM_LINE_NUMBER":
        return str(row + 1)

    elif var_name == "TM_FILENAME":
        return path.name

    elif var_name == "TM_FILENAME_BASE":
        return path.stem

    elif var_name == "TM_DIRECTORY":
        return str(path.parent)

    elif var_name == "TM_FILEPATH":
        return str(path)

    else:
        return None


# variable    ::= '$' var
def _parse_variable_naked(context: ParserCtx) -> TokenStream:
    name_acc: MutableSequence[str] = []

    for pos, char in context:
        if char in _VAR_CHARS:
            name_acc.append(char)
        else:
            name = "".join(name_acc)
            var = _variable_substitution(context, var_name=name)
            yield var if var is not None else name
            pushback_chars(context, (pos, char))
            break


# /' regex '/' (format | text)+ '/'
def _variable_decoration(
    context: ParserCtx, *, var_name: str, regex: str, fmt: str, flags: str
) -> TokenStream:
    subst = _variable_substitution(context, var_name=var_name)
    subst = var_name if subst is None else subst
    yield Unparsed(text=f"{subst}/{regex}/{fmt}/{flags}")


# | '${' var '/' regex '/' (format | text)+ '/' options '}'
def _parse_variable_decorated(context: ParserCtx, var_name: str) -> TokenStream:
    pos, char = next_char(context)
    assert char == "/"

    regex_acc: MutableSequence[str] = []
    fmt_acc: MutableSequence[str] = []
    flags_acc: MutableSequence[str] = []

    level, seen = 0, 1

    def push(char: str) -> None:
        if seen <= 1:
            regex_acc.append(char)
        elif seen == 2:
            fmt_acc.append(char)
        elif seen >= 3:
            flags_acc.append(char)

    for pos, char in context:
        if char == "\\":
            pushback_chars(context, (pos, char))
            char = _parse_escape(context, escapable_chars=_REGEX_ESC_CHARS)
            push(char)
        elif char == "/":
            if not level:
                seen += 1
            else:
                push(char)

            if seen >= 3:
                for pos, char in context:
                    if char in _REGEX_FLAG_CHARS:
                        push(char)
                    elif char == "}":
                        yield from _variable_decoration(
                            context,
                            var_name=var_name,
                            regex="".join(regex_acc),
                            fmt="".join(fmt_acc),
                            flags="".join(flags_acc),
                        )
                        return
                    else:
                        raise_err(
                            text=context.text,
                            pos=pos,
                            condition="after /../../",
                            expected=("[a-z]"),
                            actual=char,
                        )
        else:
            level += {"{": 1, "}": -1}.get(char, 0)
            push(char)


# variable    ::= '$' var | '${' var }'
#                | '${' var ':' any '}'
#                | '${' var '/' regex '/' (format | text)+ '/' options '}'
def _parse_variable_nested(context: ParserCtx) -> TokenStream:
    name_acc: MutableSequence[str] = []

    for pos, char in context:
        if char in _VAR_CHARS:
            name_acc.append(char)
        elif char == "}":
            # '${' var }'
            name = "".join(name_acc)
            var = _variable_substitution(context, var_name=name)
            yield var if var is not None else name
            break
        elif char == ":":
            # '${' var ':' any '}'
            name = "".join(name_acc)
            var = _variable_substitution(context, var_name=name)
            if var is not None:
                yield var
                context.state.depth += 1
                yield from _parse(context, shallow=True)
            else:
                yield DummyBegin()
                context.state.depth += 1
            break
        elif char == "/":
            # '${' var '/' regex '/' (format | text)+ '/' options '}'
            name = "".join(name_acc)
            pushback_chars(context, (pos, char))
            yield from _parse_variable_decorated(context, var_name=name)
            break
        else:
            raise_err(
                text=context.text,
                pos=pos,
                condition="parsing var",
                expected=("_", "a-z", "A-Z"),
                actual=char,
            )


# ${...}
def _parse_inner_scope(context: ParserCtx) -> TokenStream:
    pos, char = next_char(context)
    assert char == "{"

    pos, char = next_char(context)
    if char in _INT_CHARS:
        # tabstop | placeholder | choice
        pushback_chars(context, (pos, char))
        yield from _parse_tcp(context)
    elif char in _VAR_BEGIN_CHARS:
        # variable
        pushback_chars(context, (pos, char))
        yield from _parse_variable_nested(context)
    else:
        raise_err(
            text=context.text,
            pos=pos,
            condition="after ${",
            expected=("_", "0-9", "A-z"),
            actual=char,
        )


# $...
def _parse_scope(context: ParserCtx) -> TokenStream:
    pos, char = next_char(context)
    assert char == "$"

    pos, char = next_char(context)
    if char == "{":
        pushback_chars(context, (pos, char))
        yield from _parse_inner_scope(context)
    elif char in _INT_CHARS:
        idx_acc = [char]
        # tabstop     ::= '$' int
        for pos, char in context:
            if char in _INT_CHARS:
                idx_acc.append(char)
            else:
                yield Begin(idx=int("".join(idx_acc)))
                yield End()
                pushback_chars(context, (pos, char))
                break
    elif char in _VAR_BEGIN_CHARS:
        pushback_chars(context, (pos, char))
        yield from _parse_variable_naked(context)
    else:
        raise_err(
            text=context.text,
            pos=pos,
            condition="after $",
            expected=("{",),
            actual=char,
        )


# any         ::= tabstop | placeholder | choice | variable | text
def _parse(context: ParserCtx, shallow: bool) -> TokenStream:
    for pos, char in context:
        if char == "\\":
            pushback_chars(context, (pos, char))
            yield _parse_escape(context, escapable_chars=_ESC_CHARS)
        elif context.state.depth and char == "}":
            yield End()
            context.state.depth -= 1
            if shallow:
                break
        elif char == "$":
            pushback_chars(context, (pos, char))
            yield from _parse_scope(context)
        else:
            yield char


def parser(context: Context, info: ParseInfo, snippet: str) -> Parsed:
    ctx = context_from(snippet, context=context, info=info)
    tokens = _parse(ctx, shallow=False)
    parsed = token_parser(ctx, stream=tokens)
    return parsed
