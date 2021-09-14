from random import shuffle
from typing import Any, Mapping, MutableSequence, Optional, Sequence, cast

from pynvim_pp.logging import log

from ..shared.types import (
    UTF16,
    Completion,
    Doc,
    Edit,
    Extern,
    RangeEdit,
    SnippetEdit,
    SnippetGrammar,
    SnippetRangeEdit,
)
from .protocol import PROTOCOL
from .types import CompletionItem, CompletionResponse, LSPcomp, TextEdit


def _falsy(thing: Any) -> bool:
    return thing is None or thing == False or thing == 0 or thing == "" or thing == b""


def _range_edit(fallback: str, edit: TextEdit) -> Optional[RangeEdit]:
    rg = edit.get("range", {})
    s, e = rg.get("start", {}), rg.get("end", {})
    b_r, b_c = s.get("line"), s.get("character")
    e_r, e_c = e.get("line"), e.get("character")
    new_text = edit.get("newText")
    if (
        new_text
        and b_r is not None
        and b_c is not None
        and e_r is not None
        and e_c is not None
    ):
        begin = b_r, b_c
        end = e_r, e_c
        re = RangeEdit(
            new_text=new_text,
            fallback=fallback,
            begin=begin,
            end=end,
            encoding=UTF16,
        )
        return re
    else:
        return None


def _primary(item: CompletionItem) -> Edit:
    text_edit = item.get("textEdit")
    label = item.get("label")
    fallback = item.get("insertText") or label or ""
    re_fallback = label or fallback

    if PROTOCOL.InsertTextFormat.get(item.get("insertTextFormat")) == "Snippet":
        if isinstance(text_edit, Mapping) and "range" in text_edit:
            re = _range_edit(re_fallback, edit=cast(TextEdit, text_edit))
            if re:
                return SnippetRangeEdit(
                    grammar=SnippetGrammar.lsp,
                    new_text=re.new_text,
                    fallback=re.fallback,
                    begin=re.begin,
                    end=re.end,
                    encoding=re.encoding,
                )
            else:
                return SnippetEdit(grammar=SnippetGrammar.lsp, new_text=fallback)
        else:
            return SnippetEdit(grammar=SnippetGrammar.lsp, new_text=fallback)

    elif isinstance(text_edit, Mapping):
        # TODO -- InsertReplaceEdit
        # if "insert" in text_edit:
        #     return Edit(new_text=fall_back)
        if "range" in text_edit:
            re = _range_edit(re_fallback, edit=cast(TextEdit, text_edit))
            if re:
                return re
            else:
                return Edit(new_text=fallback)
        else:
            return Edit(new_text=fallback)
    else:
        return Edit(new_text=fallback)


def _doc(item: CompletionItem) -> Optional[Doc]:
    doc = item.get("documentation")
    detail = item.get("detail")
    if isinstance(doc, Mapping):
        markup, kind = doc.get("value"), doc.get("kind")
        if markup and kind:
            return Doc(text=markup, syntax=kind)
        else:
            return None
    elif isinstance(doc, str):
        return Doc(text=doc, syntax="")
    elif detail:
        return Doc(text=detail, syntax="")
    else:
        return None


def parse_item(
    include_extern: bool, short_name: str, weight_adjust: float, item: CompletionItem
) -> Optional[Completion]:
    if not isinstance(item, Mapping):
        return None
    else:
        label = item.get("label")
        if not label:
            return None
        else:
            p_edit = _primary(item)
            kind = PROTOCOL.CompletionItemKind.get(item.get("kind"), "")
            cmp = Completion(
                source=short_name,
                weight_adjust=weight_adjust,
                label=label,
                sort_by=item.get("filterText") or p_edit.new_text,
                primary_edit=p_edit,
                secondary_edits=tuple(
                    re
                    for edit in (item.get("additionalTextEdits") or ())
                    if (re := _range_edit("", edit=edit))
                ),
                kind=kind,
                doc=_doc(item),
                extern=(Extern.lsp, item) if include_extern else None,
                icon_match=kind,
            )
            return cmp


def parse(
    include_extern: bool,
    short_name: str,
    weight_adjust: float,
    resp: CompletionResponse,
) -> LSPcomp:
    if _falsy(resp):
        return LSPcomp(local_cache=True, items=iter(()))

    elif isinstance(resp, Mapping):
        is_complete = _falsy(resp.get("isIncomplete"))
        items = resp.get("items", [])
        shuffle(cast(MutableSequence, items))
        comps = (
            co1
            for item in items
            if (
                co1 := parse_item(
                    include_extern,
                    short_name=short_name,
                    weight_adjust=weight_adjust,
                    item=item,
                )
            )
        )
        lc = LSPcomp(local_cache=is_complete, items=comps)
        return lc

    elif isinstance(resp, Sequence) and not isinstance(cast(Any, resp), str):
        shuffle(cast(MutableSequence, resp))
        comps = (
            co2
            for item in resp
            if (
                co2 := parse_item(
                    include_extern,
                    short_name,
                    weight_adjust=weight_adjust,
                    item=item,
                )
            )
        )
        return LSPcomp(local_cache=True, items=comps)

    else:
        msg = f"Unknown LSP resp -- {type(resp)}"
        log.warn("%s", msg)
        return LSPcomp(local_cache=False, items=iter(()))
