from shopping_cli.core.catalog_text import cjk_bigrams, fts_query, fts_search_document, tokenize


def test_catalog_text_keeps_cjk_search_tokens_and_order() -> None:
    assert tokenize("西湖龙井 Tea") == ["西湖龙井", "tea"]
    assert cjk_bigrams("西湖龙井") == ["西湖", "湖龙", "龙井"]
    assert fts_search_document("西湖龙井") == "西湖龙井 西 湖 龙 井 西湖 湖龙 龙井"


def test_catalog_text_escapes_fts_phrase_quotes() -> None:
    assert fts_query('tea "box"') == '"tea" OR "box"'
    assert fts_query("茶") == '"茶"'
